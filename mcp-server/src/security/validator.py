"""
MCP Server 文件路径验证模块
"""
import os
from typing import List
from pathlib import Path

from ..config import settings


class PathValidationError(Exception):
    """路径验证异常"""
    pass


def validate_path(path: str, allowed_suffixes: List[str] = None) -> str:
    """
    验证文件路径安全性
    
    Args:
        path: 文件路径
        allowed_suffixes: 允许的文件后缀
        
    Returns:
        标准化后的绝对路径
        
    Raises:
        PathValidationError: 路径验证失败
    """
    if not path:
        raise PathValidationError("Path cannot be empty")
    
    # 标准化路径
    try:
        abs_path = os.path.abspath(os.path.expanduser(path))
    except Exception as e:
        raise PathValidationError(f"Invalid path: {e}")
    
    # 检查后缀
    if allowed_suffixes:
        suffix = os.path.splitext(abs_path)[1].lower()
        if suffix not in allowed_suffixes:
            raise PathValidationError(
                f"File type {suffix} not allowed. Allowed: {allowed_suffixes}"
            )
    
    # 检查是否在允许的路径内
    if not _is_path_allowed(abs_path):
        raise PathValidationError(
            f"Path {abs_path} is not in allowed directories"
        )
    
    # 检查是否存在
    if not os.path.exists(abs_path):
        raise PathValidationError(f"File does not exist: {abs_path}")
    
    # 检查是否是文件
    if not os.path.isfile(abs_path):
        raise PathValidationError(f"Path is not a file: {abs_path}")
    
    return abs_path


def _is_path_allowed(path: str) -> bool:
    """检查路径是否在允许的目录内"""
    # 检查是否在禁止路径内
    for denied in settings.denied_paths:
        if path.startswith(denied):
            return False
    
    # 检查是否在允许路径内
    for allowed in settings.allowed_paths:
        if path.startswith(allowed):
            return True
    
    # 如果没有配置允许路径，默认允许本地路径
    if not settings.allowed_paths:
        return True
    
    return False


def validate_base64_data(data: str, max_size_mb: int = None) -> bytes:
    """
    验证并解码 Base64 数据
    
    Args:
        data: Base64 编码的数据
        max_size_mb: 最大大小（MB）
        
    Returns:
        解码后的字节数据
    """
    import base64
    
    if not data:
        raise PathValidationError("Data cannot be empty")
    
    try:
        decoded = base64.b64decode(data)
    except Exception as e:
        raise PathValidationError(f"Invalid base64 data: {e}")
    
    if max_size_mb:
        size_mb = len(decoded) / (1024 * 1024)
        if size_mb > max_size_mb:
            raise PathValidationError(
                f"Data size {size_mb:.2f}MB exceeds limit {max_size_mb}MB"
            )
    
    return decoded


# 支持的文件类型
ALLOWED_FILE_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}

ALLOWED_SUFFIXES = list(ALLOWED_FILE_TYPES.keys())
