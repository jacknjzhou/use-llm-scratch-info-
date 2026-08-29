"""LLM 客户端：统一走 OpenAI 兼容协议，参数由 llm_config 提供。"""
import asyncio
import json
import logging
import time

from openai import AsyncOpenAI

from app.config import settings

logger = logging.getLogger(__name__)

JSON_RULES = """规则：
1. 只输出一个 JSON 对象，不要输出任何其他内容或代码块标记。
2. 文本中找不到的信息，对应字段填 null，禁止编造。
3. 每个提取到的字段同时给出 evidence（原文片段，不超过 50 字）。
输出 JSON 结构：{"field_key": {"value": ..., "evidence": "..."}, ...}"""

# ---------- 客户端复用：避免每次调用重建 httpx 连接池 ----------
_client_cache: dict = {}


def _get_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """按 (事件循环, base_url, api_key) 缓存客户端，复用底层连接池。

    worker 使用常驻事件循环，API 进程使用 uvicorn 循环，均唯一；
    缓存键持有 loop 强引用，防止 loop 对象被回收后 id 复用导致错配。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    ck = (loop, base_url, api_key)
    client = _client_cache.get(ck)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=settings.llm_timeout)
        _client_cache[ck] = client
    return client


# ---------- 全局限流：优先 Redis（跨进程生效），不可用时回退进程内 ----------
_rate_state = {"last": 0.0}
_rate_lock = asyncio.Lock()
_redis_client = None
_redis_loop = None
_redis_unavailable = False

_RATE_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == false or tonumber(cur) <= tonumber(ARGV[1]) - tonumber(ARGV[2]) then
  redis.call('SET', KEYS[1], ARGV[1], 'EX', 60)
  return 1
end
return 0
"""


async def _get_redis():
    """获取当前事件循环绑定的 Redis 客户端（连接不能跨 loop 复用）。"""
    global _redis_client, _redis_loop, _redis_unavailable
    if _redis_unavailable:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _redis_client is not None and _redis_loop is loop:
        return _redis_client
    try:
        import redis.asyncio as aioredis

        _redis_client = aioredis.from_url(settings.redis_url, socket_timeout=2)
        _redis_loop = loop
    except Exception:
        _redis_unavailable = True
        return None
    return _redis_client


async def _rate_acquire():
    """限制全局每秒 LLM 调用次数（llm_max_rps，0 = 不限流）。

    Redis 原子脚本保证多进程/多 worker 全局生效；
    Redis 不可用时回退为进程内限流（尽力而为）并只告警一次。
    """
    rps = settings.llm_max_rps
    if rps <= 0:
        return
    interval = 1.0 / rps

    r = await _get_redis()
    if r is not None:
        try:
            while True:
                now = time.time()
                ok = await r.eval(_RATE_LUA, 1, "llm_rate:last", str(now), str(interval))
                if ok:
                    return
                # 未抢到槽位：等到下一个可用时刻再试
                last = await r.get("llm_rate:last")
                wait = float(last) + interval - time.time() if last else 0.05
                await asyncio.sleep(max(min(wait, 0.5), 0.01)
                                     if wait > 0 else 0.05)
        except Exception as exc:
            global _redis_unavailable
            _redis_unavailable = True
            logger.warning("Redis 限流不可用，回退进程内限流: %s", exc)

    # 进程内回退
    async with _rate_lock:
        now = time.monotonic()
        wait = _rate_state["last"] + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        _rate_state["last"] = time.monotonic()


async def chat_json(base_url: str, api_key: str, model: str, system: str,
                    user: str, params: dict | None = None, retries: int = 2) -> dict:
    """调用 LLM 并解析 JSON 输出；response_format 不支持时自动降级重试。"""
    params = params or {}
    await _rate_acquire()
    client = _get_client(base_url, api_key)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    use_json_mode = True
    for attempt in range(retries + 1):
        try:
            kwargs = {"model": model, "messages": messages, **params}
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            resp = await client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content or ""
            # 容忍模型包裹 ```json ...```
            content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, ValueError):
            logger.warning("LLM 输出非法 JSON（第 %s 次），重试", attempt + 1)
            messages.append({"role": "assistant", "content": "输出非法，请严格只输出一个 JSON 对象。"})
        except Exception as exc:
            err = str(exc)
            if "response_format" in err or "json_object" in err:
                use_json_mode = False  # 端点不支持 JSON Mode，降级
                logger.info("端点不支持 response_format，已降级为 prompt 约束")
            if attempt >= retries:
                raise
            await asyncio.sleep(2 ** attempt)
    return {}


async def test_connectivity(base_url: str, api_key: str, model: str) -> float:
    """发送最小请求测试连通性，返回耗时毫秒。"""
    client = _get_client(base_url, api_key)
    start = time.monotonic()
    await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "回复：ok"}],
        max_tokens=5,
    )
    return round((time.monotonic() - start) * 1000)
