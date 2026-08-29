import asyncio
import io
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SessionLocal, get_db
from app.models import (ExtractResult, ExtractSchema, ExtractTask, ResultCorrection,
                        TaskFile, UploadedFile, now)
from app.tasks.celery_app import queue_for
from app.tasks.extract_run import run_extract

router = APIRouter(prefix="/api", tags=["tasks"])

TERMINAL_STATUS = {"succeeded", "failed", "partially_succeeded", "cancelled"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ExtractIn(BaseModel):
    file_ids: list[str]
    schema_id: str | None = None          # 缺省走智能匹配
    selected_fields: list[str] | None = None
    llm_config_id: str | None = None
    priority: int = 5                     # 0~9，>=8 走加急队列


@router.post("/extract")
async def create_task(body: ExtractIn, db: AsyncSession = Depends(get_db)):
    if not body.file_ids:
        raise HTTPException(400, "file_ids 不能为空")
    if not 0 <= body.priority <= 9:
        raise HTTPException(400, "priority 取值范围 0~9")
    file_ids = [uuid.UUID(x) for x in body.file_ids]
    files = (await db.execute(select(UploadedFile).where(UploadedFile.id.in_(file_ids)))).scalars().all()
    if len(files) != len(file_ids):
        raise HTTPException(404, "部分文件不存在")

    match_mode = "auto"
    schema = None
    if body.schema_id:
        match_mode = "assigned"
        schema = (await db.execute(
            select(ExtractSchema).where(ExtractSchema.id == uuid.UUID(body.schema_id)))).scalar_one_or_none()
        if schema is None:
            raise HTTPException(404, "模板不存在")

    task = ExtractTask(
        match_mode=match_mode,
        priority=body.priority,
        schema_id=schema.id if schema else None,
        llm_config_id=uuid.UUID(body.llm_config_id) if body.llm_config_id else None,
        selected_fields=body.selected_fields if match_mode == "assigned" else None,
        schema_snapshot=({"id": str(schema.id), "name": schema.name, "fields": schema.fields}
                         if schema else None),
    )
    db.add(task)
    await db.flush()
    for f in files:
        db.add(TaskFile(task_id=task.id, file_id=f.id,
                        matched_schema_id=schema.id if schema else None,
                        match_status="bound" if schema else "pending"))
    await db.commit()

    queue = queue_for(body.priority)
    celery_task = run_extract.apply_async(args=[str(task.id)], queue=queue)
    task.celery_task_id = celery_task.id
    await db.commit()
    return {"task_id": str(task.id), "match_mode": match_mode, "priority": body.priority, "queue": queue}


@router.get("/tasks")
async def list_tasks(limit: int = 20, db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ExtractTask).order_by(ExtractTask.created_at.desc()).limit(limit))).scalars().all()
    return [_dump_task(r) for r in rows]


@router.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    task = (await db.execute(select(ExtractTask).where(ExtractTask.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    data = _dump_task(task)

    corrs = (await db.execute(
        select(ResultCorrection).where(ResultCorrection.task_id == task_id))).scalars().all()
    corr_map = {(str(c.file_id), c.field_key): c for c in corrs}

    rows = (await db.execute(
        select(TaskFile, UploadedFile, ExtractResult, ExtractSchema)
        .join(UploadedFile, UploadedFile.id == TaskFile.file_id)
        .outerjoin(ExtractResult, (ExtractResult.task_id == TaskFile.task_id)
                   & (ExtractResult.file_id == TaskFile.file_id))
        .outerjoin(ExtractSchema, ExtractSchema.id == TaskFile.matched_schema_id)
        .where(TaskFile.task_id == task_id))).all()
    items = []
    for tf, f, res, sch in rows:
        labels = {x["key"]: x.get("label", x["key"]) for x in (sch.fields or [])} if sch else {}
        out_results = None
        if res and res.results:
            out_results = {}
            for k, v in res.results.items():
                item = dict(v)
                item["label"] = labels.get(k, k)
                c = corr_map.get((str(f.id), k))
                if c:
                    item["original_value"] = v.get("value")
                    item["value"] = c.corrected_value
                    item["corrected"] = True
                out_results[k] = item
        items.append({
            "file_id": str(f.id), "filename": f.filename,
            "match_status": tf.match_status, "match_confidence": float(tf.match_confidence or 0),
            "match_reason": tf.match_reason, "status": tf.status, "error": tf.error,
            "schema_id": str(tf.matched_schema_id) if tf.matched_schema_id else None,
            "schema_name": sch.name if sch else None,
            "results": out_results,
            "coverage": float(res.coverage) if res and res.coverage is not None else None,
        })
    data["files"] = items
    return data


@router.get("/tasks/{task_id}/events")
async def task_events(task_id: uuid.UUID):
    """SSE 实时推送任务状态与进度。"""
    async def gen():
        while True:
            async with SessionLocal() as db:
                task = (await db.execute(
                    select(ExtractTask).where(ExtractTask.id == task_id))).scalar_one_or_none()
                if task is None:
                    yield "event: error\ndata: task not found\n\n"
                    return
                payload = json.dumps({"status": task.status, "progress": task.progress}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                if task.status in TERMINAL_STATUS:
                    return
            await asyncio.sleep(1.5)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class CorrectionIn(BaseModel):
    field_key: str
    value: object
    label: str | None = None


@router.post("/tasks/{task_id}/files/{file_id}/corrections")
async def add_correction(task_id: uuid.UUID, file_id: uuid.UUID, body: CorrectionIn,
                         db: AsyncSession = Depends(get_db)):
    """记录人工修正；作为同模板后续提取的 few-shot 参考。"""
    tf = (await db.execute(select(TaskFile).where(
        TaskFile.task_id == task_id, TaskFile.file_id == file_id))).scalar_one_or_none()
    if tf is None:
        raise HTTPException(404, "任务中不存在该文件")
    original = None
    snippet = None
    res = (await db.execute(select(ExtractResult).where(
        ExtractResult.task_id == task_id, ExtractResult.file_id == file_id))).scalar_one_or_none()
    if res and res.results and body.field_key in res.results:
        original = res.results[body.field_key].get("value")
        snippet = res.results[body.field_key].get("evidence")
    db.add(ResultCorrection(
        task_id=task_id, file_id=file_id, schema_id=tf.matched_schema_id,
        field_key=body.field_key, field_label=body.label,
        original_value={"value": original} if original is not None else None,
        corrected_value=body.value, snippet=snippet))
    await db.commit()
    return {"ok": True, "corrected_value": body.value}


@router.post("/tasks/{task_id}/files/{file_id}/reassign")
async def reassign(task_id: uuid.UUID, file_id: uuid.UUID, body: dict,
                   db: AsyncSession = Depends(get_db)):
    """手动改派模板并只重跑该文件（不影响任务内其他已成功文件）。"""
    sid = body.get("schema_id")
    if not sid:
        raise HTTPException(400, "schema_id 必填")
    schema = (await db.execute(
        select(ExtractSchema).where(ExtractSchema.id == uuid.UUID(sid)))).scalar_one_or_none()
    if schema is None:
        raise HTTPException(404, "模板不存在")
    tf = (await db.execute(select(TaskFile).where(
        TaskFile.task_id == task_id, TaskFile.file_id == file_id))).scalar_one_or_none()
    if tf is None:
        raise HTTPException(404, "任务中不存在该文件")
    task = (await db.execute(select(ExtractTask).where(ExtractTask.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status == "running":
        raise HTTPException(409, "任务正在运行，请等待完成后再改派")
    tf.matched_schema_id = schema.id
    tf.match_status = "bound"
    tf.match_confidence = 1.0
    tf.match_reason = "用户手动指定"
    tf.status = "pending"
    tf.error = None
    task.status = "pending"
    task.error = None
    await db.commit()
    # 只重跑该文件：worker 会跳过其他已成功文件
    run_extract.apply_async(args=[str(task_id), str(file_id)], queue=queue_for(task.priority))
    return {"reassigned": True, "rerun_file_only": True}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """取消任务：worker 在下一个文件边界检测到后停止。"""
    task = (await db.execute(select(ExtractTask).where(ExtractTask.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status in TERMINAL_STATUS:
        raise HTTPException(400, f"任务已结束（{task.status}），无法取消")
    task.status = "cancelled"
    task.finished_at = now()
    await db.commit()
    return {"cancelled": True}


@router.get("/tasks/{task_id}/export")
async def export_task(task_id: uuid.UUID, format: str = "json",
                      db: AsyncSession = Depends(get_db)):
    """导出：format=json（默认）或 xlsx。"""
    data = await get_task(task_id, db)
    if format != "xlsx":
        return {"task": data["id"], "status": data["status"], "match_mode": data["match_mode"],
                "files": data["files"]}

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "提取结果"
    ws.append(["文件名", "模板", "文件状态", "覆盖率", "字段 key", "字段名称", "提取值", "原文依据", "人工修正"])
    for item in data["files"]:
        results = item.get("results") or {}
        if not results:
            ws.append([item["filename"], item.get("schema_name") or "", item.get("status") or "",
                       item.get("coverage"), "", "", "", "", ""])
            continue
        for k, v in results.items():
            val = v.get("value")
            if isinstance(val, list):
                val = "；".join(str(x) for x in val)
            ws.append([
                item["filename"], item.get("schema_name") or "", item.get("status") or "",
                item.get("coverage"), k, v.get("label", k), val, v.get("evidence"),
                "是" if v.get("corrected") else "",
            ])
    buf = io.BytesIO()
    wb.save(buf)
    filename = f"task_{task_id}.xlsx"
    return StreamingResponse(
        io.BytesIO(buf.getvalue()), media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _dump_task(r: ExtractTask) -> dict:
    return {
        "id": str(r.id), "match_mode": r.match_mode, "priority": r.priority, "status": r.status,
        "progress": r.progress, "created_at": str(r.created_at), "finished_at": str(r.finished_at),
        "error": r.error, "schema_id": str(r.schema_id) if r.schema_id else None,
    }
