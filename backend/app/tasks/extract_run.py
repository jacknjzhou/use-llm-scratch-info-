"""Celery 任务：多文件提取编排（分类预判 → 逐文件提取 → 状态回写）。

可靠性设计：
- 抢锁：UPDATE ... WHERE status='pending' 原子抢占，防止 reassign/acks_late 重投导致双跑；
- 幂等：已 succeeded 的文件跳过；改派重跑只处理目标文件（only_file_id）；
- 取消：每个文件处理前检查任务是否被取消；
- 心跳：每处理完一个文件更新 heartbeat，供 API 启动时识别僵死任务。
"""
import asyncio
import logging
import uuid

from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.models import ExtractTask, ExtractSchema, TaskFile, UploadedFile, now
from app.services import classify_file, extract_file, get_default_llm_config, recompute_task_status
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _claim_task(db, tid: uuid.UUID) -> ExtractTask | None:
    """原子抢占任务（pending → running）。抢不到说明已被其他 worker 执行，直接放弃。"""
    claim = (await db.execute(
        update(ExtractTask)
        .where(ExtractTask.id == tid, ExtractTask.status == "pending")
        .values(status="running", heartbeat=now(), total_chunks=0, done_chunks=0)
        .returning(ExtractTask.id))).scalar_one_or_none()
    if claim is None:
        return None
    await db.commit()
    return (await db.execute(select(ExtractTask).where(ExtractTask.id == tid))).scalar_one()


async def _is_cancelled(db, tid) -> bool:
    status = (await db.execute(
        select(ExtractTask.status).where(ExtractTask.id == tid))).scalar_one_or_none()
    return status != "running"


async def _run(task_id: str, only_file_id: str | None = None) -> None:
    tid = uuid.UUID(task_id)
    only_fid = uuid.UUID(only_file_id) if only_file_id else None
    async with SessionLocal() as db:
        task = await _claim_task(db, tid)
        if task is None:
            logger.info("任务 %s 抢锁失败（非 pending 或已被执行），跳过", task_id)
            return

        try:
            tf_rows = (await db.execute(select(TaskFile).where(TaskFile.task_id == tid))).scalars().all()
            llm_cfg = await get_default_llm_config(db, task.llm_config_id)
            # assigned 模式下仅提取用户勾选的字段
            selected = set(task.selected_fields) if task.selected_fields else None

            # 智能匹配模式：仅对尚未分类的文件做预判（重跑时保留用户手动改派结果）
            if task.match_mode == "auto":
                schemas = (await db.execute(
                    select(ExtractSchema).where(ExtractSchema.is_deleted.is_(False)))).scalars().all()
                for tf in tf_rows:
                    if tf.match_status != "pending":
                        continue
                    if only_fid is not None and tf.file_id != only_fid:
                        continue
                    file_row = (await db.execute(
                        select(UploadedFile).where(UploadedFile.id == tf.file_id))).scalar_one()
                    try:
                        match = await classify_file(file_row, schemas, llm_cfg, db)
                    except Exception as exc:
                        logger.warning("分类失败 %s: %s", tf.file_id, exc)
                        tf.match_status, tf.match_reason = "unmatched", f"分类调用失败: {exc}"
                        await db.commit()
                        continue
                    tf.match_confidence = match["confidence"]
                    tf.match_reason = match["reason"]
                    sid = match["schema_id"]
                    
                    # 验证 schema_id 是否存在（防止 LLM 编造 UUID）
                    schema_ids = {s.id for s in schemas}
                    if sid is not None and sid not in schema_ids:
                        logger.warning("LLM 返回了不存在的 schema_id: %s，使用 None 替代", sid)
                        sid = None
                    
                    tf.matched_schema_id = sid
                    if sid is None or match["confidence"] < settings.match_confirm_threshold:
                        tf.match_status = "unmatched"
                    elif match["confidence"] < settings.match_auto_threshold:
                        tf.match_status = "need_confirm"
                    else:
                        tf.match_status = "bound"
                    await db.commit()

            # 逐文件提取（文件级失败隔离；已成功的文件跳过，保证重跑幂等）
            schema_cache: dict = {}
            for tf in tf_rows:
                # 只重跑指定文件（reassign 场景）；跳过已成功文件
                if only_fid is not None and tf.file_id != only_fid:
                    continue
                if tf.status == "succeeded":
                    continue
                # 每个文件前检查取消/状态
                if await _is_cancelled(db, tid):
                    logger.info("任务 %s 已取消或状态变更，停止处理", task_id)
                    return

                if tf.match_status == "unmatched" or tf.matched_schema_id is None:
                    tf.status = "failed"
                    tf.error = "未匹配到模板，请在结果页手动指定后重跑"
                    await db.commit()
                    continue
                if tf.matched_schema_id not in schema_cache:
                    schema_cache[tf.matched_schema_id] = (await db.execute(
                        select(ExtractSchema).where(ExtractSchema.id == tf.matched_schema_id))).scalar_one()
                schema = schema_cache[tf.matched_schema_id]
                file_row = (await db.execute(
                    select(UploadedFile).where(UploadedFile.id == tf.file_id))).scalar_one()

                tf.status = "running"
                await db.commit()
                try:
                    stats = await extract_file(tid, file_row, schema, llm_cfg, db, selected_fields=selected)
                    tf.status = "succeeded"
                    tf.error = None
                    task.total_chunks += stats["chunks"]
                    task.done_chunks += stats["chunks"] - stats["failed_chunks"]
                except Exception as exc:
                    logger.exception("文件提取失败 %s", tf.file_id)
                    tf.status = "failed"
                    tf.error = str(exc)[:1000]
                    file_row.status = "failed"
                # 进度按全部文件的终态占比计算（重跑场景下同样准确）
                finished = sum(1 for t in tf_rows if t.status in ("succeeded", "failed"))
                task.progress = int(finished / max(len(tf_rows), 1) * 100)
                task.heartbeat = now()
                await db.commit()

            # 汇总最终状态（依据全部文件状态，而非本次处理的子集）
            if await _is_cancelled(db, tid):
                logger.info("任务 %s 已取消，保留 cancelled 状态", task_id)
                return
            await recompute_task_status(db, task)
            task.error = None
        except Exception as exc:
            logger.exception("任务失败 %s", task_id)
            task.status = "failed"
            task.error = str(exc)[:1000]
        task.finished_at = now()
        await db.commit()


_worker_loop: asyncio.AbstractEventLoop | None = None


def _get_worker_loop() -> asyncio.AbstractEventLoop:
    """worker 进程内复用常驻事件循环，避免 asyncio.run 反复建/关 loop 导致
    数据库连接池跨 loop 复用（"Future attached to a different loop"）。"""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    return _worker_loop


@celery_app.task(name="extract.run")
def run_extract(task_id: str, only_file_id: str | None = None) -> str:
    _get_worker_loop().run_until_complete(_run(task_id, only_file_id))
    return task_id
