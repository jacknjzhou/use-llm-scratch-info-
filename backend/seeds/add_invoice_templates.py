#!/usr/bin/env python3
"""手动添加发票模板脚本

使用方法：
    cd backend
    python seeds/add_invoice_templates.py

或通过 Docker：
    docker exec -it <container> python seeds/add_invoice_templates.py
"""
import asyncio
import json
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.database import SessionLocal, async_engine
from app.models import ExtractSchema


async def add_invoice_templates():
    """添加发票模板（幂等：已存在则跳过）。"""
    seed_path = Path(__file__).parent / "preset_templates.json"
    if not seed_path.exists():
        print(f"错误：未找到模板文件 {seed_path}")
        return

    data = json.loads(seed_path.read_text(encoding="utf-8"))
    
    # 只获取发票模板
    invoice_templates = [t for t in data["templates"] 
                         if t["name"] in ("增值税专用发票", "普通发票")]

    async with SessionLocal() as db:
        added = 0
        skipped = 0
        
        for tpl in invoice_templates:
            # 检查是否已存在
            existing = (await db.execute(
                select(ExtractSchema).where(ExtractSchema.name == tpl["name"])
            )).scalar_one_or_none()
            
            if existing:
                print(f"跳过（已存在）：{tpl['name']}")
                skipped += 1
                continue
            
            # 插入新模板
            schema = ExtractSchema(
                name=tpl["name"],
                category=tpl.get("category", "发票类"),
                description=tpl.get("description", ""),
                fields=tpl["fields"],
                is_preset=True
            )
            db.add(schema)
            print(f"添加：{tpl['name']} ({len(tpl['fields'])} 个字段)")
            added += 1
        
        await db.commit()
        
        print(f"\n完成：新增 {added} 个，跳过 {skipped} 个")
        return added


if __name__ == "__main__":
    count = asyncio.run(add_invoice_templates())
    sys.exit(0 if count is not None else 1)
