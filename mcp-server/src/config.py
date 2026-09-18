"""
MCP Server 配置模块
"""
import os
from typing import List
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """应用配置"""
    
    # 数据库
    database_url: str = os.getenv(
        "DATABASE_URL", 
        "postgresql+asyncpg://extractor:extractor@postgres:5432/extractor"
    )
    
    # Backend API（复用主服务）
    api_base_url: str = os.getenv("API_BASE_URL", "http://api:8000")
    
    # 存储
    storage_dir: str = os.getenv("STORAGE_DIR", "/app/storage")
    
    # API 认证（与主服务保持一致）
    api_key: str = os.getenv("API_KEY", "")
    
    # 文件处理
    max_file_size_mb: int = int(os.getenv("UPLOAD_MAX_MB", "50"))
    ocr_lang: str = os.getenv("OCR_LANG", "chi_sim+eng")
    classify_max_pages: int = 5
    
    # MCP 服务
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8080
    mcp_mode: str = "stdio"
    
    # 安全设置
    allowed_paths: List[str] = ["/data", "/tmp", "/app/storage"]
    denied_paths: List[str] = ["/etc", "/var", "/root", "/proc", "/sys", "/app/.git"]
    
    # 任务超时（秒）
    task_timeout: int = 120
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


# 全局配置实例
settings = Settings()
