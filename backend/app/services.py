"""提取核心：智能模板匹配 + chunk 并发提取 + 结果合并。"""
import asyncio
import logging
import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crypto import decrypt_api_key
from app.llm_client import JSON_RULES, chat_json
from app.models import (ExtractResult, ExtractSchema, ExtractTask, LlmConfig,
                        ResultCorrection, TaskFile, UploadedFile)
from app.parsers import parse_file
from app.storage import get_storage

logger = logging.getLogger(__name__)


async def load_few_shot_examples(db: AsyncSession, schema_id, limit: int = 8) -> str:
    """取该模板最近的人工修正记录，拼成 few-shot 示例段（为空时返回空串）。"""
    rows = (await db.execute(
        select(ResultCorrection)
        .where(ResultCorrection.schema_id == schema_id)
        .order_by(ResultCorrection.created_at.desc())
        .limit(limit))).scalars().all()
    if not rows:
        return ""
    lines = ["参考以下经人工核对的提取示例（遇到类似情况请按此输出）："]
    for r in rows:
        label = r.field_label or r.field_key
        snippet = (r.snippet or "").strip()
        lines.append(f"- 字段「{label}」：原文片段『{snippet[:60]}』→ 应提取为 {r.corrected_value}")
    return "\n".join(lines) + "\n"


def _normalize_value(value, ftype: str):
    """按字段类型归一化 LLM 返回值。"""
    if value is None:
        return None
    try:
        if ftype == "number":
            if isinstance(value, str):
                value = value.replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
            return float(value)
        if ftype == "date":
            s = str(value).strip()
            return s[:10] if len(s) >= 10 and s[4] == "-" and s[7] == "-" else s
        if ftype == "boolean":
            return bool(value)
    except (TypeError, ValueError):
        return None
    return value


def merge_chunk_results(field_defs: list[dict], chunk_outputs: list[dict]) -> dict:
    """合并多 chunk 结果：非空优先；同字段多值保留为列表。"""
    merged: dict[str, dict] = {}
    for f in field_defs:
        key = f["key"]
        values = []  # (value, evidence, chunk_index, page)
        for out in chunk_outputs:
            item = out.get(key)
            if not isinstance(item, dict):
                continue
            value = _normalize_value(item.get("value"), f.get("type", "string"))
            if value is None:
                continue
            values.append({"value": value, "evidence": item.get("evidence"),
                           "chunk": out.get("_chunk"), "page": out.get("_page")})
        if not values:
            merged[key] = {"value": None, "evidence": None}
        elif len({str(v["value"]) for v in values}) == 1:
            merged[key] = values[0]
        else:
            merged[key] = {"value": [v["value"] for v in values], "evidence": values[0]["evidence"],
                           "options": values}
    return merged


async def extract_file(task_id, file_row: UploadedFile, schema: ExtractSchema,
                       llm_cfg: LlmConfig, db: AsyncSession, selected_fields: set | None = None) -> dict:
    """提取单个文件：解析分块 → 并发调 LLM（注入 few-shot）→ 合并 → 写结果。

    写入前先删除该 (task, file) 的旧结果，保证重跑（改派模板等）幂等，不出重复行。
    """
    field_defs = schema.fields
    if selected_fields:
        field_defs = [f for f in field_defs if f.get("key") in selected_fields]
    if not field_defs:
        raise ValueError("没有可提取的字段（勾选列表为空）")
    local_path = get_storage().resolve(file_row.storage_path)
    chunks = parse_file(local_path)
    few_shot = await load_few_shot_examples(db, schema.id)

    # prompt 与并发块无关，构建一次复用
    fields_desc = "\n".join(
        f"- {f['key']} ({f.get('type', 'string')}): {f.get('label', '')} —— {f.get('desc', '')}"
        for f in field_defs)
    system = f"你是信息提取助手。从给定文本片段中，严格按照 fields 定义提取信息。\n\nfields:\n{fields_desc}\n\n{few_shot}\n{JSON_RULES}"

    semaphore = asyncio.Semaphore(settings.llm_concurrency)

    async def extract_chunk(chunk: dict) -> dict:
        user = f"待提取文本（第 {chunk['page']} 页）：\n<<<\n{chunk['text']}\n>>>"
        async with semaphore:
            data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                                   llm_cfg.model, system, user, llm_cfg.params)
        data["_chunk"] = chunk["index"]
        data["_page"] = chunk["page"]
        return data

    outputs = await asyncio.gather(*(extract_chunk(c) for c in chunks), return_exceptions=True)
    ok_outputs = []
    errors = 0
    last_error = ""
    for o in outputs:
        if isinstance(o, Exception):
            errors += 1
            last_error = str(o)
            logger.warning("chunk 提取失败: %s", o)
        else:
            ok_outputs.append(o)
    if not ok_outputs and errors > 0:
        raise RuntimeError(f"所有分块提取均失败: {last_error[:500]}")

    results = merge_chunk_results(field_defs, ok_outputs)
    total = len(field_defs)
    found = sum(1 for v in results.values() if v["value"] is not None)
    coverage = round(found / total, 2) if total else 0.0

    # 重跑幂等：upsert 覆盖旧结果（(task_id, file_id) 唯一），不会出现重复行
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = pg_insert(ExtractResult).values(
        id=uuid.uuid4(), task_id=task_id, file_id=file_row.id,
        results=results, coverage=coverage).on_conflict_do_update(
        constraint="uq_result_task_file",
        set_={"results": results, "coverage": coverage})
    await db.execute(stmt)
    file_row.status = "parsed"
    return {"coverage": coverage, "chunks": len(chunks), "failed_chunks": errors}


async def classify_file(file_row: UploadedFile, schemas: list[ExtractSchema],
                        llm_cfg: LlmConfig) -> dict:
    """智能匹配：只解析前两页文本（避免整本扫描件 OCR），判断归属模板。

    返回 {schema_id, confidence, reason}。
    """
    head_text = ""
    try:
        local_path = get_storage().resolve(file_row.storage_path)
        chunks = parse_file(local_path, max_pages=2)
        head_text = "\n".join(c["text"] for c in chunks[:2])[:3000]
    except Exception as exc:
        logger.warning("分类前解析失败 %s: %s", file_row.filename, exc)

    candidates = "\n".join(
        f"- id: {s.id}\n  name: {s.name}\n  category: {s.category or ''}\n  description: {s.description or ''}"
        for s in schemas)
    system = (
        "你是文件类型分类器。根据给定的文件名和文本片段，判断该文件最符合哪个模板类别。\n"
        "只输出 JSON：{\"schema_id\": \"...\", \"confidence\": 0~1, \"reason\": \"一句话理由\"}\n"
        "若都不匹配，schema_id 填 null。禁止编造。")
    user = f"候选模板：\n{candidates}\n\n文件名：{file_row.filename}\n\n文件片段：\n<<<\n{head_text}\n>>>"

    data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                           llm_cfg.model, system, user)
    schema_id = data.get("schema_id")
    if schema_id:
        try:
            schema_id = uuid.UUID(str(schema_id))
        except ValueError:
            schema_id = None
    return {
        "schema_id": schema_id,
        "confidence": float(data.get("confidence") or 0),
        "reason": data.get("reason"),
    }


async def get_default_llm_config(db: AsyncSession, config_id=None) -> LlmConfig:
    q = select(LlmConfig).where(LlmConfig.enabled.is_(True))
    if config_id:
        q = q.where(LlmConfig.id == config_id)
    else:
        q = q.where(LlmConfig.is_default.is_(True))
    cfg = (await db.execute(q.limit(1))).scalar_one_or_none()
    if cfg is None and config_id is None:
        cfg = (await db.execute(
            select(LlmConfig).where(LlmConfig.enabled.is_(True)).limit(1))).scalar_one_or_none()
    if cfg is None:
        raise RuntimeError("未配置可用的 LLM，请先在模型配置页添加")
    return cfg


async def recompute_task_status(db: AsyncSession, task: ExtractTask) -> None:
    """按 task_file 的最终状态汇总任务状态（重跑/取消后调用，保证与文件状态一致）。"""
    rows = (await db.execute(
        select(TaskFile.status).where(TaskFile.task_id == task.id))).scalars().all()
    if not rows:
        task.status = "failed"
        task.error = "任务中没有文件"
        return
    ok = sum(1 for s in rows if s == "succeeded")
    fail = len(rows) - ok
    if fail == 0:
        task.status = "succeeded"
    elif ok == 0:
        task.status = "failed"
    else:
        task.status = "partially_succeeded"



# ============================================================
# 阶段一/二增强：智能多页采样 + 增强 Prompt 分类
# ============================================================

def sample_text_from_chunks(chunks: list[dict], max_chars: int | None = None,
                            strategy: str | None = None) -> str:
    """
    从分块中智能采样文本用于分类。

    Args:
        chunks: parse_file 返回的分块列表
        max_chars: 最大字符数（默认使用配置）
        strategy: 采样策略（默认使用配置）
            - "head_middle_tail": 前中后各取一段
            - "content_rich": 优先选取内容密集的分块
            - "hybrid": 结合两种策略

    Returns:
        采样后的文本
    """
    if not chunks:
        return ""

    max_chars = max_chars or settings.classify_max_chars
    strategy = strategy or settings.classify_sample_strategy
    total_pages = max((c.get("page", 1) for c in chunks), default=1)

    if strategy == "head_middle_tail":
        samples = []
        # 头部：前2页
        head_chunks = [c for c in chunks if c.get("page", 1) <= 2]
        for c in head_chunks:
            if sum(len(s) for s in samples) + len(c["text"]) <= max_chars // 3:
                samples.append(c["text"])

        # 中部：中间位置
        if total_pages > 4:
            mid_page = max(2, total_pages // 2)
            mid_chunks = [c for c in chunks if abs(c.get("page", 1) - mid_page) <= 1]
            for c in mid_chunks[:2]:
                if sum(len(s) for s in samples) + len(c["text"]) <= max_chars // 3:
                    samples.append(c["text"])

        # 尾部：最后1页
        tail_chunks = [c for c in chunks if c.get("page", 1) >= total_pages - 1]
        for c in tail_chunks[:2]:
            if sum(len(s) for s in samples) + len(c["text"]) <= max_chars // 3:
                samples.append(c["text"])

        return "\n...\n".join(samples)

    elif strategy == "content_rich":
        sorted_chunks = sorted(chunks, key=lambda c: len(c.get("text", "")), reverse=True)
        samples = []
        for c in sorted_chunks:
            text = c.get("text", "")
            if sum(len(s) for s in samples) + len(text) <= max_chars:
                samples.append(text)
            if sum(len(s) for s in samples) >= max_chars * 0.9:
                break
        return "\n".join(samples)

    else:  # hybrid
        samples = []
        head_text = "\n".join(c["text"] for c in chunks if c.get("page", 1) <= 2)
        samples.append(head_text)

        if len(chunks) > 4:
            sorted_by_len = sorted(chunks[4:], key=lambda c: len(c.get("text", "")), reverse=True)
            if sorted_by_len:
                rich_text = sorted_by_len[0].get("text", "")
                if len("\n".join(samples)) + len(rich_text) <= max_chars:
                    samples.append(f"[中间关键页]\n{rich_text}")

        return "\n...\n".join(samples)


async def load_classification_examples(db: AsyncSession, schemas: list[ExtractSchema],
                                       limit: int = 5) -> str:
    """加载历史成功分类案例作为 few-shot 示例。"""
    if not schemas:
        return ""

    schema_ids = [s.id for s in schemas]
    rows = (await db.execute(
        select(TaskFile)
        .where(TaskFile.matched_schema_id.in_(schema_ids))
        .where(TaskFile.match_status.in_(["bound", "need_confirm"]))
        .where(TaskFile.match_confidence >= 0.7)
        .order_by(TaskFile.task_id.desc())
        .limit(limit)
    )).scalars().all()

    if not rows:
        return ""

    file_ids = [r.file_id for r in rows]
    files = (await db.execute(
        select(UploadedFile).where(UploadedFile.id.in_(file_ids))
    )).scalars().all()
    file_map = {f.id: f for f in files}

    schema_map = {s.id: s for s in schemas}
    examples = ["参考以下历史分类案例："]
    for r in rows:
        f = file_map.get(r.file_id)
        s = schema_map.get(r.matched_schema_id)
        if f and s:
            examples.append(
                f"- 文件「{f.filename}」→ 模板「{s.name}」(置信度: {r.match_confidence:.2f})"
            )

    return "\n".join(examples) + "\n"


async def classify_file_v2(file_row: UploadedFile, schemas: list[ExtractSchema],
                           llm_cfg: LlmConfig, db: AsyncSession) -> dict:
    """
    增强版智能匹配：多页采样 + 增强 Prompt + 历史示例。

    返回 {schema_id, confidence, reason, matching_features}。
    """
    # 1. 多页采样
    sampled_text = ""
    try:
        local_path = get_storage().resolve(file_row.storage_path)
        chunks = parse_file(local_path, max_pages=settings.classify_max_pages)
        sampled_text = sample_text_from_chunks(chunks)
    except Exception as exc:
        logger.warning("分类前解析失败 %s: %s", file_row.filename, exc)

    if not sampled_text:
        return {"schema_id": None, "confidence": 0.0, "reason": "文档解析失败，无法分类"}

    # 2. 构建增强的候选模板信息（包含关键字段）
    candidates = []
    for s in schemas:
        key_fields = [
            {"key": f.get("key", ""), "label": f.get("label", ""), "type": f.get("type", "string")}
            for f in (s.fields or [])[:5]  # 取前5个关键字段
        ]
        candidates.append({
            "id": str(s.id),
            "name": s.name,
            "category": s.category or "未分类",
            "description": (s.description or "")[:200],  # 限制描述长度
            "key_fields": key_fields
        })

    # 3. 获取历史分类示例
    few_shot = await load_classification_examples(db, schemas, limit=5)

    # 4. 增强的系统 Prompt
    system = f"""你是专业的文档分类专家。根据给定的文档片段，判断其最符合哪个模板类别。

分类原则：
1. 优先依据文档中的关键字段/特征词判断（如合同编号、发票号码、金额格式、日期格式）
2. 参考模板的字段定义，匹配度最高的优先
3. 注意文档的结构特征（合同条款、表格格式、标题层级等）
4. 若文档片段不足以判断，confidence 设为较低值而非返回 null

{few_shot}

输出要求：严格输出 JSON，格式如下：
{{
  "schema_id": "匹配的模板UUID字符串，未找到填 null",
  "confidence": 0.0~1.0,
  "reason": "详细判断理由（20字以上）",
  "matching_features": ["匹配到的特征1", "匹配到的特征2"]
}}"""

    # 5. 用户 Prompt
    user = f"""候选模板列表：
{json.dumps(candidates, ensure_ascii=False, indent=2)}

文件名：{file_row.filename}

文档片段：
<<<
{sampled_text}
>>>"""

    # 6. 调用 LLM
    data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                           llm_cfg.model, system, user)

    # 7. 解析结果
    schema_id = data.get("schema_id")
    if schema_id:
        try:
            schema_id = uuid.UUID(str(schema_id))
        except ValueError:
            schema_id = None
            logger.warning("LLM 返回的 schema_id 格式无效: %s", data.get("schema_id"))

    return {
        "schema_id": schema_id,
        "confidence": float(data.get("confidence") or 0),
        "reason": data.get("reason"),
        "matching_features": data.get("matching_features", [])
    }


# ============================================================
# 兼容层：默认使用增强版分类
# ============================================================

async def classify_file(file_row: UploadedFile, schemas: list[ExtractSchema],
                        llm_cfg: LlmConfig, db: AsyncSession | None = None) -> dict:
    """
    智能匹配入口函数。

    - 若 db 不为 None，使用增强版（classify_file_v2）
    - 否则使用原版（保持向后兼容）
    """
    if db is not None:
        return await classify_file_v2(file_row, schemas, llm_cfg, db)

    # 原版实现（保持向后兼容）
    head_text = ""
    try:
        local_path = get_storage().resolve(file_row.storage_path)
        chunks = parse_file(local_path, max_pages=2)
        head_text = "\n".join(c["text"] for c in chunks[:2])[:3000]
    except Exception as exc:
        logger.warning("分类前解析失败 %s: %s", file_row.filename, exc)

    candidates = "\n".join(
        f"- id: {s.id}\n  name: {s.name}\n  category: {s.category or ''}\n  description: {s.description or ''}"
        for s in schemas)
    system = (
        "你是文件类型分类器。根据给定的文件名和文本片段，判断该文件最符合哪个模板类别。\n"
        "只输出 JSON：{{\"schema_id\": \"...\", \"confidence\": 0~1, \"reason\": \"一句话理由\"}}\n"
        "若都不匹配，schema_id 填 null。禁止编造。")
    user = f"候选模板：\n{candidates}\n\n文件名：{file_row.filename}\n\n文件片段：\n<<<\n{head_text}\n>>>"

    data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                           llm_cfg.model, system, user)
    schema_id = data.get("schema_id")
    if schema_id:
        try:
            schema_id = uuid.UUID(str(schema_id))
        except ValueError:
            schema_id = None
    return {
        "schema_id": schema_id,
        "confidence": float(data.get("confidence") or 0),
        "reason": data.get("reason"),
    }
