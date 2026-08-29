import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ExtractSchema

router = APIRouter(prefix="/api/schemas", tags=["schemas"])

FIELD_TYPES = {"string", "number", "date", "boolean", "enum", "array"}


class FieldIn(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    type: str = "string"
    desc: str = ""


class SchemaIn(BaseModel):
    name: str
    category: str | None = None
    description: str | None = None
    fields: list[FieldIn]


async def _get(db: AsyncSession, sid) -> ExtractSchema:
    row = (await db.execute(
        select(ExtractSchema).where(ExtractSchema.id == sid, ExtractSchema.is_deleted.is_(False))
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "模板不存在")
    return row


@router.post("")
async def create_schema(body: SchemaIn, db: AsyncSession = Depends(get_db)):
    _check_fields(body.fields)
    row = ExtractSchema(name=body.name, category=body.category, description=body.description,
                        fields=[f.model_dump() for f in body.fields])
    db.add(row)
    await db.commit()
    return _dump(row)


@router.get("")
async def list_schemas(category: str | None = None, db: AsyncSession = Depends(get_db)):
    q = select(ExtractSchema).where(ExtractSchema.is_deleted.is_(False)).order_by(ExtractSchema.created_at)
    if category:
        q = q.where(ExtractSchema.category == category)
    rows = (await db.execute(q)).scalars().all()
    return [_dump(r) for r in rows]


@router.get("/{sid}")
async def get_schema(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return _dump(await _get(db, sid))


@router.put("/{sid}")
async def update_schema(sid: uuid.UUID, body: SchemaIn, db: AsyncSession = Depends(get_db)):
    row = await _get(db, sid)
    if row.is_preset:
        raise HTTPException(400, "预置模板不可修改，请复制后修改副本")
    _check_fields(body.fields)
    row.name, row.category, row.description = body.name, body.category, body.description
    row.fields = [f.model_dump() for f in body.fields]
    row.version += 1
    await db.commit()
    return _dump(row)


@router.post("/{sid}/copy")
async def copy_schema(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    src = await _get(db, sid)
    row = ExtractSchema(name=f"{src.name}（副本）", category=src.category, description=src.description,
                        fields=src.fields, is_preset=False)
    db.add(row)
    await db.commit()
    return _dump(row)


@router.delete("/{sid}")
async def delete_schema(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    row = await _get(db, sid)
    if row.is_preset:
        raise HTTPException(400, "预置模板不可删除")
    row.is_deleted = True
    await db.commit()
    return {"deleted": True}


@router.post("/import")
async def import_schemas(body: dict, db: AsyncSession = Depends(get_db)):
    """导入模板 JSON：支持单模板对象 {name,category,description,fields} 或批量 {templates:[...]}。"""
    items = body.get("templates") if isinstance(body.get("templates"), list) else [body]
    if not items:
        raise HTTPException(400, "没有可导入的模板")
    created = []
    for t in items:
        if not isinstance(t, dict) or not t.get("name") or not isinstance(t.get("fields"), list):
            raise HTTPException(400, f"模板格式不正确: {t.get('name', '?')}")
        _check_fields([FieldIn(**f) if isinstance(f, dict) else f for f in t["fields"]])
        row = ExtractSchema(
            name=t["name"], category=t.get("category"), description=t.get("description"),
            fields=t["fields"], is_preset=False)
        db.add(row)
        await db.flush()
        created.append(str(row.id))
    await db.commit()
    return {"created": created}


@router.get("/{sid}/export")
async def export_schema(sid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return _dump(await _get(db, sid))


def _check_fields(fields: list[FieldIn]):
    if not fields:
        raise HTTPException(400, "模板至少需要一个字段")
    keys = {f.key for f in fields}
    if len(keys) != len(fields):
        raise HTTPException(400, "字段 key 重复")
    for f in fields:
        if f.type not in FIELD_TYPES:
            raise HTTPException(400, f"不支持的字段类型: {f.type}")


def _dump(r: ExtractSchema) -> dict:
    return {
        "id": str(r.id), "name": r.name, "category": r.category, "description": r.description,
        "fields": r.fields, "version": r.version, "is_preset": r.is_preset, "updated_at": str(r.updated_at),
    }
