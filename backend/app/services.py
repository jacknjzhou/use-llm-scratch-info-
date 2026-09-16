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
            for f in (s.fields or [])[:8]  # 取前5个关键字段
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
1. 优先依据文档中的关键字段/特征词判断（如文件内容、合同、行程单、发票号码、采购、发票、住宿），锁定关键信息
2. 参考模板的字段定义，匹配度最高的优先
3. 注意文档的结构特征（合同条款、标题层级等）
4. 若文档片段不足以判断，confidence 设为较低值而非返回 null

{few_shot}

输出要求：严格输出 JSON，格式如下：
{{
  "schema_id": "匹配的模板UUID字符串，未找到填 null",
  "confidence": 0.0~1.0,
  "reason": "详细判断理由（50字以上,100字内）",
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

    # 通过 schema_name 查找真实 UUID（避免 LLM 编造 UUID）
    schema_name = data.get("schema_name", "")
    matched_schema_name = None
    if schema_id is None and schema_name:
        for s in schemas:
            if s.name == schema_name:
                schema_id = s.id
                matched_schema_name = s.name
                break
    
    # 如果通过 UUID 找到了 schema，获取其名称
    if matched_schema_name is None and schema_id:
        for s in schemas:
            if s.id == schema_id:
                matched_schema_name = s.name
                break

    return {
        "schema_id": schema_id,
        "schema_name": matched_schema_name,
        "confidence": float(data.get("confidence") or 0) if data.get("confidence") is not None else 0.5,
        "reason": data.get("reason") or "分类完成",
        "matching_features": data.get("matching_features") or []
    }


# ============================================================
# 阶段三：两阶段分类（粗筛 + 精排）
# ============================================================

async def _coarse_classify(schemas: list[ExtractSchema], filename: str,
                           text: str, llm_cfg: LlmConfig) -> list[dict]:
    """
    阶段一：粗筛 - 快速排除明显不匹配的模板。

    使用轻量 Prompt，返回置信度 > 0.3 的候选模板（最多5个）。
    """
    template_list = "\n".join([
        f"{i+1}. {s.name} (类别: {s.category or '未分类'})"
        for i, s in enumerate(schemas)
    ])

    system = """你是一个文档分类助手。根据文件名和文档片段，快速判断该文档可能属于哪个模板类别。

输出要求：严格输出 JSON 数组，格式如下：
[
  {"index": 1, "name": "模板名", "confidence": 0.0~1.0},
  ...
]
- 返回置信度 > 0.3 的模板，最多返回 5 个
- 如果所有模板都不匹配，则返回置信度最高的一个，否则返回空数组 []
- 禁止编造，只输出你确定的判断"""

    user = f"""文件名：{filename}

文档片段：
<<<
{text[:3000]}
>>>

模板列表：
{template_list}"""

    try:
        data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                               llm_cfg.model, system, user)
        candidates = data if isinstance(data, list) else []
    except Exception as exc:
        logger.warning("粗筛调用失败: %s", exc)
        return []

    # 映射到完整 schema
    schema_map = {s.name: s for s in schemas}
    results = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("template_name")
        schema = schema_map.get(name)
        if schema:
            results.append({
                "schema": schema,
                "confidence": float(item.get("confidence", 0)),
                "reason": f"粗筛匹配: {name}"
            })

    return sorted(results, key=lambda x: x["confidence"], reverse=True)[:5]


async def _fine_classify(candidates: list[dict], filename: str,
                         text: str, llm_cfg: LlmConfig,
                         db: AsyncSession | None) -> dict:
    """
    阶段二：精排 - 详细分析候选模板。

    使用增强 Prompt 输出最终匹配结果。
    """
    if not candidates:
        return {"schema_id": None, "confidence": 0.0, "reason": "无候选模板", "_candidates": [], "matching_features": []}

    # 只有一个候选，直接返回（带微调置信度）
    if len(candidates) == 1:
        c = candidates[0]
        return {
            "schema_id": c["schema"].id,
            "confidence": min(c["confidence"] * 1.05, 0.95),  # 轻微提升
            "reason": c.get("reason", "唯一候选模板"),
            "_candidates": candidates
        }

    # 多个候选，进行精排
    candidate_detail = "\n".join([
        f"- id: {c['schema'].id}\n  name: {c['schema'].name}\n  category: {c['schema'].category or ''}\n  description: {(c['schema'].description or '')[:200]}\n  key_fields: {json.dumps([{'key': f.get('key', ''), 'label': f.get('label', ''), 'type': f.get('type', 'string')} for f in (c['schema'].fields or [])[:3]], ensure_ascii=False)}"
        for c in candidates
    ])

    # 获取 few-shot 示例
    few_shot = ""
    if db:
        few_shot = await load_classification_examples(
            db, [c["schema"] for c in candidates], limit=3
        )

    system = f"""你是专业的文档分类专家。根据给定的文档片段，从候选模板中选择最匹配的一个。

分类原则：
1. 仔细比对文档中的关键字段/特征词与模板字段定义的匹配度
2. 关注文档的结构特征（合同条款格式、发票格式、表格结构等）
3. 优先选择特征词匹配最多的模板
4. 如果候选模板都很接近，选择置信度最高的那个

{few_shot}

输出要求：严格输出 JSON 对象：
{{"schema_name": "模板名称（如：普通发票）", "confidence": 0.0~1.0, "reason": "详细判断理由（20字以上）", "matching_features": ["匹配特征1", "匹配特征2"]}}"""

    user = f"""候选模板详情：
{candidate_detail}

文件名：{filename}

文档片段：
<<<
{text}
>>>"""

    try:
        data = await chat_json(llm_cfg.base_url, decrypt_api_key(llm_cfg.api_key_enc),
                               llm_cfg.model, system, user)
    except Exception as exc:
        logger.warning("精排调用失败: %s", exc)
        # 回退到粗筛结果
        top = max(candidates, key=lambda x: x["confidence"])
        return {
            "schema_id": top["schema"].id,
            "confidence": top["confidence"] * 0.8,  # 降权
            "reason": f"精排失败，使用粗筛结果: {top.get('reason', '')}",
            "_candidates": candidates
        }

    # 通过 schema_name 查找真实 UUID（避免 LLM 编造 UUID）
    schema_id = None
    schema_name = data.get("schema_name", "")
    for c in candidates:
        if c["schema"].name == schema_name:
            schema_id = c["schema"].id
            break
    
    # 如果名称匹配失败，尝试置信度最高的候选
    if schema_id is None and candidates:
        logger.warning("精筛无法通过名称匹配 schema_name=%s，使用置信度最高的候选", schema_name)
        schema_id = candidates[0]["schema"].id

    # 获取匹配的模板名称
    matched_schema_name = None
    if schema_id:
        for c in candidates:
            if c["schema"].id == schema_id:
                matched_schema_name = c["schema"].name
                break
    
    return {
        "schema_id": schema_id,
        "schema_name": matched_schema_name,
        "confidence": float(data.get("confidence") or 0) if data.get("confidence") is not None else 0.5,
        "reason": data.get("reason") or "精排完成",
        "matching_features": data.get("matching_features") or [],
        "_candidates": candidates
    }


async def classify_file_two_stage(file_row: UploadedFile, schemas: list[ExtractSchema],
                                   llm_cfg: LlmConfig, db: AsyncSession) -> dict:
    """
    两阶段分类入口：粗筛 + 精排。

    适用于模板数量较多（>10个）的场景。
    """
    # 1. 多页采样
    sampled_text = ""
    try:
        local_path = get_storage().resolve(file_row.storage_path)
        chunks = parse_file(local_path, max_pages=settings.classify_max_pages)
        sampled_text = sample_text_from_chunks(chunks)
    except Exception as exc:
        logger.warning("分类前解析失败 %s: %s", file_row.filename, exc)
        return {"schema_id": None, "confidence": 0.0, "reason": f"文档解析失败: {exc}"}

    if not sampled_text:
        return {"schema_id": None, "confidence": 0.0, "reason": "文档解析为空", "_candidates": [], "matching_features": []}

    # 2. 阶段一：粗筛（模板数量 > 5 时启用）
    if len(schemas) > 5:
        coarse_candidates = await _coarse_classify(
            schemas, file_row.filename, sampled_text, llm_cfg
        )
        if not coarse_candidates:
            # 粗筛无候选时，使用所有模板作为候选继续精排（避免漏掉）
            logger.warning("粗筛无候选模板，降级使用全部模板: %s", file_row.filename)
            coarse_candidates = [{"schema": s, "confidence": 0.5} for s in schemas]
    else:
        # 模板较少时，跳过粗筛
        coarse_candidates = [{"schema": s, "confidence": 0.5} for s in schemas]

    # 3. 阶段二：精排
    result = await _fine_classify(
        coarse_candidates, file_row.filename, sampled_text, llm_cfg, db
    )
    # 确保返回结果包含 _candidates
    if "_candidates" not in result:
        result["_candidates"] = coarse_candidates
    return result


# ============================================================
# 阶段四：动态阈值 + 回退机制
# ============================================================

def calculate_dynamic_threshold(candidates: list[dict], base_threshold: float = 0.8) -> tuple[float, str]:
    """
    根据候选模板的分布动态调整阈值。

    Args:
        candidates: 候选模板列表
        base_threshold: 基础阈值

    Returns:
        (threshold, strategy): 调整后的阈值和策略描述
    """
    if not candidates:
        return 0.5, "无候选，使用默认阈值"

    if len(candidates) == 1:
        # 单一候选，降低阈值提高自动绑定率
        return base_threshold * 0.9, "单一候选，降低阈值"

    top_conf = candidates[0].get("confidence", 0)
    second_conf = candidates[1].get("confidence", 0) if len(candidates) > 1 else 0
    gap = top_conf - second_conf

    if gap > 0.3:
        # 第一名明显领先，提高阈值避免误匹配
        threshold = min(base_threshold * 1.1, 0.95)
        strategy = "候选差距大，提高阈值"
    elif gap < 0.1:
        # 候选接近，降低阈值倾向保守
        threshold = base_threshold * 0.85
        strategy = "候选差距小，降低阈值"
    else:
        threshold = base_threshold
        strategy = "候选差距适中，使用默认阈值"

    return threshold, strategy


async def classify_with_fallback(file_row: UploadedFile, schemas: list[ExtractSchema],
                                 llm_cfg: LlmConfig, db: AsyncSession) -> dict:
    """
    带重试和回退的分类。

    流程：
    1. 尝试两阶段分类
    2. 动态阈值判断
    3. 失败时重试（最多2次）
    4. 所有失败回退到 need_confirm
    """
    max_retries = settings.match_max_retries
    last_error = None

    for attempt in range(max_retries):
        try:
            # 使用两阶段分类
            result = await classify_file_two_stage(file_row, schemas, llm_cfg, db)

            # 动态阈值判断
            candidates = result.get("_candidates", [])
            threshold, strategy = calculate_dynamic_threshold(candidates)
            confidence = result["confidence"]
            result["threshold_strategy"] = strategy

            # 根据动态阈值确定最终状态
            if result["schema_id"] is None or confidence < 0.3:
                result["match_status"] = "unmatched"
            elif confidence < threshold:
                result["match_status"] = "need_confirm"
            else:
                result["match_status"] = "bound"

            return result

        except Exception as exc:
            last_error = exc
            logger.warning("分类尝试 %s 失败: %s", attempt + 1, exc)

            if attempt < max_retries - 1:
                await asyncio.sleep(settings.match_retry_delay)
                continue

    # 所有重试都失败，返回 need_confirm 而非 unmatched
    return {
        "schema_id": None,
        "confidence": 0.0,
        "reason": f"分类服务异常: {last_error}",
        "match_status": "need_confirm",
        "threshold_strategy": "重试失败，使用默认状态",
        "_candidates": [],  # 确保有 _candidates 字段
        "matching_features": []
    }


# ============================================================
# 阶段五：历史缓存 + 批量优化
# ============================================================

class MatchCache:
    """分类结果内存缓存（进程内）。"""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 86400):
        self._cache: dict[str, dict] = {}
        self._timestamps: dict[str, float] = {}
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds

    def _make_key(self, filename: str, file_hash: str | None = None) -> str:
        """生成缓存键（标准化文件名）。"""
        import re
        normalized = re.sub(r'\d{4,}', 'N', filename)  # 替换连续数字
        normalized = re.sub(r'V\d+', 'V', normalized)   # 替换版本号
        key = f"{normalized}:{file_hash or 'unknown'}"
        return key

    def get(self, filename: str, file_hash: str | None = None) -> dict | None:
        """获取缓存的分类结果。"""
        key = self._make_key(filename, file_hash)

        if key in self._timestamps:
            if __import__('time').time() - self._timestamps[key] > self.ttl_seconds:
                del self._cache[key]
                del self._timestamps[key]
                return None

        return self._cache.get(key)

    def set(self, filename: str, file_hash: str | None, result: dict):
        """缓存分类结果。"""
        key = self._make_key(filename, file_hash)

        # LRU 淘汰
        if len(self._cache) >= self.max_size and key not in self._cache:
            oldest_key = min(self._timestamps, key=self._timestamps.get)
            del self._cache[oldest_key]
            del self._timestamps[oldest_key]

        self._cache[key] = result
        self._timestamps[key] = __import__('time').time()

    def clear(self):
        """清空缓存。"""
        self._cache.clear()
        self._timestamps.clear()


# 全局缓存实例
_match_cache = MatchCache()


def normalize_filename(filename: str) -> str:
    """标准化文件名用于相似度比较。"""
    import re
    normalized = re.sub(r'\d{4,}', 'N', filename)
    normalized = re.sub(r'V\d+', 'V', normalized)
    normalized = re.sub(r'[_\-\s]+', '', normalized).lower()
    return normalized


def calculate_filename_similarity(s1: str, s2: str) -> float:
    """计算文件名相似度（字符重叠率）。"""
    set1, set2 = set(s1), set(s2)
    if not set1 or not set2:
        return 0.0
    overlap = len(set1 & set2)
    return overlap / max(len(set1), len(set2))


async def classify_file_with_cache(file_row: UploadedFile, schemas: list[ExtractSchema],
                                   llm_cfg: LlmConfig, db: AsyncSession,
                                   file_hash: str | None = None) -> dict:
    """
    带缓存的分类。

    - 优先从缓存获取
    - 缓存未命中时执行实际分类
    - 仅缓存高置信度（>=0.7）的结果
    """
    # 尝试从缓存获取
    cached = _match_cache.get(file_row.filename, file_hash)
    if cached:
        logger.info("分类缓存命中: %s", file_row.filename)
        return {**cached, "from_cache": True}

    # 执行实际分类
    result = await classify_with_fallback(file_row, schemas, llm_cfg, db)

    # 缓存高置信度结果
    if result.get("confidence", 0) >= 0.7 and result.get("schema_id"):
        _match_cache.set(file_row.filename, file_hash, result)

    return {**result, "from_cache": False}


# ============================================================
# 最终入口：统一使用增强版分类
# ============================================================

async def classify_file(file_row: UploadedFile, schemas: list[ExtractSchema],
                        llm_cfg: LlmConfig, db: AsyncSession | None = None) -> dict:
    """
    智能匹配入口函数（最终版）。

    使用完整增强流程：
    - 两阶段分类（模板 > 5 时）
    - 动态阈值
    - 历史缓存
    - 重试回退
    """
    logger.info(f"[分类开始] 文件: {file_row.filename}, 模板数: {len(schemas)}")
    try:
        if db is not None and settings.match_cache_enabled:
            result = await classify_file_with_cache(file_row, schemas, llm_cfg, db)
        elif db is not None:
            result = await classify_with_fallback(file_row, schemas, llm_cfg, db)
        else:
            result = await classify_file_v2(file_row, schemas, llm_cfg, db)
        # 日志输出时处理 UUID 序列化问题
        # log_result = {
        #     k: str(v) if isinstance(v, uuid.UUID) else v
        #     for k, v in result.items()
        # }
        # logger.info(f"Classify Info:{json.dumps(log_result)}")
        # 确保返回结果完整
        result.setdefault("_candidates", [])
        result.setdefault("matching_features", [])
        result.setdefault("reason", "分类完成")
        
        logger.info(f"[分类完成] 文件: {file_row.filename}, schema_id: {result.get('schema_id')}, "
                   f"confidence: {result.get('confidence')}, reason: {str(result.get('reason', ''))[:50]}")
        return result
        
    except Exception as exc:
        logger.error("分类入口异常: %s", exc)
        return {
            "schema_id": None,
            "confidence": 0.0,
            "reason": f"分类异常: {str(exc)[:100]}",
            "match_status": "need_confirm",
            "_candidates": [],
            "matching_features": []
        }
