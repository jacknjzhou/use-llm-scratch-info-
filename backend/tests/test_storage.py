"""存储后端与加密测试。"""
from pathlib import Path

from app.crypto import decrypt_api_key, encrypt_api_key
from app.storage import clear_storage_cache, get_storage


def test_local_storage_roundtrip(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_dir", str(tmp_path))
    clear_storage_cache()
    try:
        s = get_storage()
        key = s.put_bytes(b"hello doc", "uploads/t.txt")
        assert Path(s.resolve(key)).read_bytes() == b"hello doc"
    finally:
        clear_storage_cache()


def test_crypto_roundtrip():
    secret = "sk-very-secret-key-123"
    enc = encrypt_api_key(secret)
    assert enc != secret
    assert decrypt_api_key(enc) == secret
