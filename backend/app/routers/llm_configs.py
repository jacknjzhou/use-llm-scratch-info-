import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_api_key, encrypt_api_key
from app.database import get_db
from app.llm_client import test_connectivity
from app.models import LlmConfig

router = APIRouter(prefix="/api/llm-configs", tags=["llm-configs"])


class LlmConfigIn(BaseModel):
    name: str
    base_url: str
    api_key: str | None = None  # 修改时可不传，保留原 key
    model: str
    params: dict = {}
    is_default: bool = False


async def _get(db: AsyncSession, cid) -> LlmConfig:
    row = (await db.execute(select(LlmConfig).where(LlmConfig.id == cid))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "配置不存在")
    return row


@router.post("")
async def create_config(body: LlmConfigIn, db: AsyncSession = Depends(get_db)):
    if not body.api_key:
        raise HTTPException(400, "api_key 必填")
    if body.is_default:
        await _clear_default(db)
    row = LlmConfig(name=body.name, base_url=body.base_url, model=body.model,
                    api_key_enc=encrypt_api_key(body.api_key), params=body.params,
                    is_default=body.is_default)
    db.add(row)
    await db.commit()
    return _dump(row)


@router.get("")
async def list_configs(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(LlmConfig).order_by(LlmConfig.created_at))).scalars().all()
    return [_dump(r) for r in rows]


@router.put("/{cid}")
async def update_config(cid: uuid.UUID, body: LlmConfigIn, db: AsyncSession = Depends(get_db)):
    row = await _get(db, cid)
    if body.is_default:
        await _clear_default(db)
    row.name, row.base_url, row.model, row.params = body.name, body.base_url, body.model, body.params
    row.is_default = body.is_default
    if body.api_key:
        row.api_key_enc = encrypt_api_key(body.api_key)
    await db.commit()
    return _dump(row)


@router.delete("/{cid}")
async def delete_config(cid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await _get(db, cid)
    await db.delete(row)
    await db.commit()
    return {"deleted": True}


@router.post("/{cid}/test")
async def test_config(cid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await _get(db, cid)
    try:
        ms = await test_connectivity(row.base_url, decrypt_api_key(row.api_key_enc), row.model)
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}
    return {"ok": True, "latency_ms": ms}


async def _clear_default(db: AsyncSession):
    rows = (await db.execute(select(LlmConfig).where(LlmConfig.is_default.is_(True)))).scalars().all()
    for r in rows:
        r.is_default = False


def _dump(r: LlmConfig) -> dict:
    return {
        "id": str(r.id), "name": r.name, "provider": r.provider, "base_url": r.base_url,
        "model": r.model, "params": r.params, "is_default": r.is_default, "enabled": r.enabled,
        "has_key": True,
    }
