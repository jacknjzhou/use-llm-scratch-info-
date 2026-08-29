"""提取合并逻辑测试：非空优先、冲突留列表、类型归一、null 未找到。"""
import asyncio

from app.llm_client import _rate_acquire
from app.services import merge_chunk_results


def test_merge_single_value_dedup():
    fields = [{"key": "amount", "type": "number"}, {"key": "sign_date", "type": "date"},
              {"key": "party_a", "type": "string"}]
    outputs = [
        {"amount": {"value": "1,200,000", "evidence": "总金额120万"}, "sign_date": {"value": None},
         "_chunk": 0},
        {"amount": {"value": "1200000", "evidence": "金额1200000"}, "sign_date": {"value": "2026-08-01"},
         "_chunk": 1},
    ]
    merged = merge_chunk_results(fields, outputs)
    assert merged["amount"]["value"] == 1200000.0          # 千分位去逗号 + 去重单值
    assert merged["sign_date"]["value"] == "2026-08-01"
    assert merged["party_a"]["value"] is None              # 找不到 → null
    assert merged["party_a"]["evidence"] is None


def test_merge_conflict_keeps_list():
    fields = [{"key": "amount", "type": "number"}]
    outputs = [
        {"amount": {"value": "100", "evidence": "a"}, "_chunk": 0},
        {"amount": {"value": "200", "evidence": "b"}, "_chunk": 1},
    ]
    merged = merge_chunk_results(fields, outputs)
    assert merged["amount"]["value"] == [100.0, 200.0]     # 冲突保留列表
    assert len(merged["amount"]["options"]) == 2


def test_normalize_currency_and_units():
    fields = [{"key": "amount", "type": "number"}]
    merged = merge_chunk_results(fields, [{"amount": {"value": "￥553.0元", "evidence": ""}, "_chunk": 0}])
    assert merged["amount"]["value"] == 553.0


def test_rate_limiter_enforces_rps():
    """llm_max_rps=20 → 5 次调用至少耗时 4 个间隔（约 200ms）。"""
    from app.config import settings
    import time

    old = settings.llm_max_rps
    settings.llm_max_rps = 20
    try:
        t0 = time.monotonic()
        for _ in range(5):
            asyncio.run(_rate_acquire())
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.18, f"限流未生效: {elapsed:.3f}s"
    finally:
        settings.llm_max_rps = old


def test_rate_limiter_disabled_is_fast():
    from app.config import settings
    import time

    old = settings.llm_max_rps
    settings.llm_max_rps = 0
    try:
        t0 = time.monotonic()
        for _ in range(5):
            asyncio.run(_rate_acquire())
        assert time.monotonic() - t0 < 0.05
    finally:
        settings.llm_max_rps = old
