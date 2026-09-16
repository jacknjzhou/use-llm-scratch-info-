import json
import logging
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select, update

from app.config import settings
from app.database import SessionLocal, engine
from app.models import Base, ExtractTask, now
from app.routers import files, llm_configs, schemas, tasks
from app.tasks.celery_app import queue_for
from app.tasks.extract_run import run_extract

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def seed_preset_templates():
    """首次启动写入预置模板（幂等：新模板逐个检查追加）。"""
    from app.models import ExtractSchema

    seed_path = Path(__file__).resolve().parent.parent / "seeds" / "preset_templates.json"
    if not seed_path.exists():
        logger.warning("未找到预置模板文件: %s", seed_path)
        return
    data = json.loads(seed_path.read_text(encoding="utf-8"))
    async with SessionLocal() as db:
        added = 0
        for tpl in data["templates"]:
            # 逐个检查模板是否已存在
            existing = (await db.execute(
                select(ExtractSchema).where(ExtractSchema.name == tpl["name"])
            )).scalar_one_or_none()
            if existing:
                continue
            db.add(ExtractSchema(
                name=tpl["name"], category=tpl.get("category"),
                description=tpl.get("description"), fields=tpl["fields"], is_preset=True))
            added += 1
        await db.commit()
        if added > 0:
            logger.info("已新增 %s 套预置模板（当前共 %s 套）", added, len(data["templates"]))


async def recover_stale_tasks():
    """启动时恢复任务：
    - pending：队列消息可能丢失（如 Redis 重置），重新入队；
    - running 且心跳超时：worker 已死亡，重置为 pending 并重新入队。
    """
    async with SessionLocal() as db:
        stale_before = now() - timedelta(minutes=settings.task_stale_minutes)

        pend_rows = (await db.execute(
            select(ExtractTask.id, ExtractTask.priority)
            .where(ExtractTask.status == "pending"))).all()
        stale_rows = (await db.execute(
            select(ExtractTask.id, ExtractTask.priority)
            .where(ExtractTask.status == "running",
                   or_(ExtractTask.heartbeat.is_(None),
                       ExtractTask.heartbeat < stale_before)))).all()

        if stale_rows:
            stale_ids = [r.id for r in stale_rows]
            await db.execute(update(ExtractTask)
                             .where(ExtractTask.id.in_(stale_ids))
                             .values(status="pending", error=None))
            await db.commit()

        for tid, priority in pend_rows + stale_rows:
            run_extract.apply_async(args=[str(tid)], queue=queue_for(priority))
        if pend_rows or stale_rows:
            logger.info("启动恢复：重新入队 %s 个 pending 任务，重置 %s 个僵死任务",
                        len(pend_rows), len(stale_rows))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await seed_preset_templates()
    try:
        await recover_stale_tasks()
    except Exception as exc:
        logger.warning("启动任务恢复失败（不影响 API 启动）: %s", exc)
    yield


app = FastAPI(title="Doc Intelligence - 智能信息提取系统", lifespan=lifespan)


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    """可选 API 认证：设置了 API_KEY 环境变量后，所有 /api 请求需携带
    X-API-Key 头或 ?api_key= 查询参数（SSE EventSource 无法设头，走查询参数）。"""
    if settings.api_key and request.url.path.startswith("/api"):
        provided = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
        if provided != settings.api_key:
            return JSONResponse({"detail": "无效的 API Key"}, status_code=401)
    return await call_next(request)


app.include_router(files.router)
app.include_router(schemas.router)
app.include_router(llm_configs.router)
app.include_router(tasks.router)


@app.get("/api/health")
async def health():
    return {"ok": True}
