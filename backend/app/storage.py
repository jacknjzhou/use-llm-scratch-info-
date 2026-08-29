"""存储后端抽象：local（docker volume，默认）或 s3（兼容 OSS/MinIO）。

统一两个能力：
- put_bytes(data, key) -> key
- resolve(key) -> 本地可读路径（s3 会按需下载到缓存目录）
"""
import logging
import shutil
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)


class Storage:
    def put_bytes(self, data: bytes, key: str) -> str:
        raise NotImplementedError

    def resolve(self, key: str) -> str:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError


class LocalStorage(Storage):
    def __init__(self):
        self.root = Path(settings.storage_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, key: str) -> str:
        dest = self.root / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return key

    def resolve(self, key: str) -> str:
        return str(self.root / key)

    def delete(self, key: str) -> None:
        (self.root / key).unlink(missing_ok=True)


class S3Storage(Storage):
    """boto3 客户端，兼容 AWS S3 / 阿里云 OSS / MinIO（通过 endpoint 切换）。"""

    def __init__(self):
        import boto3

        if not settings.s3_bucket:
            raise RuntimeError("storage_backend=s3 但未配置 S3_BUCKET")
        session = boto3.session.Session(
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
            region_name=settings.s3_region,
        )
        self.client = session.client(
            "s3",
            endpoint_url=settings.s3_endpoint or None,
            config=boto3.session.Config(signature_version="s3v4"),
        )
        self.bucket = settings.s3_bucket
        self.cache = Path(settings.storage_dir) / "s3cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, data: bytes, key: str) -> str:
        import io

        self.client.upload_fileobj(io.BytesIO(data), self.bucket, key)
        return key

    def resolve(self, key: str) -> str:
        local = self.cache / key
        if not local.exists():
            local.parent.mkdir(parents=True, exist_ok=True)
            self.client.download_file(self.bucket, key, str(local))
            logger.info("已从 S3 下载 %s -> %s", key, local)
        return str(local)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
        local = self.cache / key
        if local.exists():
            local.unlink(missing_ok=True)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        if settings.storage_backend == "s3":
            _storage = S3Storage()
        else:
            _storage = LocalStorage()
    return _storage


def clear_storage_cache():
    """测试用：重置单例。"""
    global _storage
    _storage = None
