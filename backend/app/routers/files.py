import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import UploadedFile
from app.storage import get_storage

router = APIRouter(prefix="/api/files", tags=["files"])

ALLOWED = {".pdf", ".docx", ".xlsx", ".txt", ".csv", ".md", ".png", ".jpg", ".jpeg"}


@router.post("")
async def upload_files(files: list[UploadFile], db: AsyncSession = Depends(get_db)):
    """多文件上传，返回 file_id 列表。存储后端由 STORAGE_BACKEND 决定。"""
    storage = get_storage()
    max_bytes = settings.upload_max_mb * 1024 * 1024
    result = []
    written_keys: list[str] = []  # 中途失败时回滚已写入存储的文件，避免孤儿对象
    try:
        for f in files:
            suffix = Path(f.filename or "").suffix.lower()
            if suffix not in ALLOWED:
                raise HTTPException(400, f"不支持的文件格式: {f.filename}（支持 {sorted(ALLOWED)}）")
            file_id = uuid.uuid4()
            key = f"uploads/{file_id}{suffix}"
            data = await f.read()
            if len(data) > max_bytes:
                raise HTTPException(400, f"文件过大: {f.filename}（上限 {settings.upload_max_mb} MB）")
            storage.put_bytes(data, key)
            written_keys.append(key)
            row = UploadedFile(
                id=file_id,
                filename=Path(f.filename).name,
                storage_path=key,
                mime_type=f.content_type,
                size_bytes=len(data),
            )
            db.add(row)
            result.append({"file_id": str(file_id), "filename": row.filename, "size": row.size_bytes})
        await db.commit()
    except Exception:
        for key in written_keys:
            try:
                storage.delete(key)
            except Exception:
                pass  # 清理失败不影响主错误返回
        await db.rollback()
        raise
    return result
