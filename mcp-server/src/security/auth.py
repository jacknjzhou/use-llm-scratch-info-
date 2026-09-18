"""
MCP Server 认证模块
"""
import hashlib
import hmac
from typing import Optional
from functools import wraps

from ..config import settings


class AuthError(Exception):
    """认证异常"""
    pass


def verify_api_key(api_key: Optional[str]) -> bool:
    """
    验证 API Key
    
    Args:
        api_key: 待验证的 API Key
        
    Returns:
        验证是否通过
    """
    if not settings.api_key:
        # 未配置 API Key，跳过验证
        return True
    
    if not api_key:
        return False
    
    return hmac.compare_digest(
        hashlib.sha256(api_key.encode()).hexdigest(),
        hashlib.sha256(settings.api_key.encode()).hexdigest()
    )


def require_auth(func):
    """认证装饰器"""
    @wraps(func)
    async def wrapper(*args, api_key: Optional[str] = None, **kwargs):
        if not verify_api_key(api_key):
            raise AuthError("Invalid API Key")
        return await func(*args, **kwargs)
    return wrapper
