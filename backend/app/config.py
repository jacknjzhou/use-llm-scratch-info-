from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://extractor:extractor@postgres:5432/extractor"
    redis_url: str = "redis://redis:6379/0"
    storage_dir: str = "/app/storage"
    llm_master_key: str = "change-me"

    chunk_size: int = 1800
    chunk_overlap: int = 200
    llm_concurrency: int = 5
    llm_timeout: int = 120
    llm_max_rps: float = 0            # 0 = 不限流；>0 时限制每秒 LLM 调用次数（Redis 全局生效）
    ocr_lang: str = "chi_sim+eng"
    ocr_dpi: int = 200                 # PDF 扫描页渲染 DPI
    ocr_retry_dpi: int = 300           # 低置信重试 DPI
    ocr_psm: int = 3                   # tesseract 页面分割模式
    ocr_min_chars: int = 15            # OCR 结果低于该字符数时升 DPI 重试

    # 存储后端：local（默认，docker volume）或 s3（兼容 OSS/MinIO）
    storage_backend: str = "local"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"

    # 智能匹配置信度阈值
    match_auto_threshold: float = 0.8
    match_confirm_threshold: float = 0.5

    # 上传限制：单文件最大 MB 数
    upload_max_mb: int = 50

    # 僵死任务判定：running 状态心跳超过该分钟数视为 worker 已死亡，启动时重置并重新入队
    task_stale_minutes: int = 30

    # 数据库连接池（worker 建议调小：DB_POOL_SIZE=2）
    db_pool_size: int = 5
    db_max_overflow: int = 10

    # 可选 API 认证：设置后所有 /api 请求需携带 X-API-Key 头或 ?api_key= 参数
    api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
