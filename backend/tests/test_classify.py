"""智能分类功能单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

# 被测试的函数
from app.services import (
    sample_text_from_chunks,
    calculate_dynamic_threshold,
    normalize_filename,
    calculate_filename_similarity,
    MatchCache,
)


class TestSampleTextFromChunks:
    """测试 sample_text_from_chunks 函数"""

    def get_mock_settings(self, **kwargs):
        """创建模拟的 settings"""
        settings = MagicMock()
        settings.classify_max_chars = 5000
        settings.classify_sample_strategy = "head_middle_tail"
        settings.update = lambda **u: None
        for k, v in kwargs.items():
            setattr(settings, k, v)
        return settings

    def test_head_middle_tail_strategy(self):
        """测试前中后采样策略"""
        chunks = [
            {"page": 1, "text": "第1页内容" * 100},
            {"page": 2, "text": "第2页内容" * 100},
            {"page": 5, "text": "第5页内容" * 100},
            {"page": 10, "text": "第10页内容" * 100},
        ]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks(chunks)
            
            assert len(result) > 0
            assert "第1页" in result or "第2页" in result

    def test_empty_chunks(self):
        """测试空分块列表"""
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks([])
            assert result == ""

    def test_single_chunk(self):
        """测试单个分块"""
        chunks = [{"page": 1, "text": "单页内容"}]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks(chunks)
            assert "单页内容" in result

    def test_content_rich_strategy(self):
        """测试内容密集策略"""
        chunks = [
            {"page": 1, "text": "短内容"},
            {"page": 2, "text": "中等内容" * 50},
            {"page": 3, "text": "超长内容" * 200},
        ]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "content_rich"
            
            result = sample_text_from_chunks(chunks, strategy="content_rich")
            assert "超长内容" in result

    def test_max_chars_limit(self):
        """测试最大字符数限制"""
        chunks = [
            {"page": 1, "text": "第1页" * 1000},
            {"page": 2, "text": "第2页" * 1000},
        ]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 500
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks(chunks, max_chars=500)
            assert len(result) <= 600  # 允许一定溢出


class TestDynamicThreshold:
    """测试 calculate_dynamic_threshold 函数"""

    def test_single_candidate_lowers_threshold(self):
        """单一候选应降低阈值"""
        candidates = [{"schema_id": "1", "confidence": 0.8}]
        threshold, strategy = calculate_dynamic_threshold(candidates)
        
        assert threshold < 0.8
        assert "单一候选" in strategy

    def test_large_gap_increases_threshold(self):
        """候选差距大时应提高阈值"""
        candidates = [
            {"schema_id": "1", "confidence": 0.9},
            {"schema_id": "2", "confidence": 0.4},
        ]
        threshold, strategy = calculate_dynamic_threshold(candidates)
        
        assert threshold > 0.8
        assert "差距大" in strategy

    def test_small_gap_decreases_threshold(self):
        """候选差距小时应降低阈值"""
        candidates = [
            {"schema_id": "1", "confidence": 0.75},
            {"schema_id": "2", "confidence": 0.7},
        ]
        threshold, strategy = calculate_dynamic_threshold(candidates)
        
        assert threshold < 0.8
        assert "差距小" in strategy

    def test_moderate_gap_uses_default(self):
        """候选差距适中时使用默认阈值"""
        candidates = [
            {"schema_id": "1", "confidence": 0.85},
            {"schema_id": "2", "confidence": 0.6},
        ]
        threshold, strategy = calculate_dynamic_threshold(candidates)
        
        assert threshold == 0.8
        assert "默认" in strategy

    def test_empty_candidates(self):
        """空候选应返回默认阈值"""
        threshold, strategy = calculate_dynamic_threshold([])
        
        assert threshold == 0.5
        assert "无候选" in strategy

    def test_threshold_max_limit(self):
        """阈值不应超过 0.95"""
        candidates = [
            {"schema_id": "1", "confidence": 1.0},
            {"schema_id": "2", "confidence": 0.0},
        ]
        threshold, strategy = calculate_dynamic_threshold(candidates)
        
        assert threshold <= 0.95


class TestFilenameMatching:
    """测试文件名规范化与相似度计算"""

    def test_normalize_removes_numbers(self):
        """规范化应替换连续数字"""
        assert "N" in normalize_filename("合同2024001.pdf")
        assert "V1" not in normalize_filename("文件V2.docx")
        assert "V" in normalize_filename("文件V2.docx")

    def test_normalize_lowercase(self):
        """规范化应转为小写"""
        result = normalize_filename("CONTRACT.DOCX")
        assert result.islower()

    def test_normalize_remove_separators(self):
        """规范化应移除分隔符"""
        result = normalize_filename("合同_模板-2024.pdf")
        assert "_" not in result
        assert "-" not in result

    def test_similarity_identical(self):
        """完全相同的文件名相似度为1"""
        sim = calculate_filename_similarity("合同模板", "合同模板")
        assert sim == 1.0

    def test_similarity_partial(self):
        """部分相似的文件名"""
        sim = calculate_filename_similarity("合同模板", "合同模板2024")
        assert 0.5 < sim < 1.0

    def test_similarity_different(self):
        """完全不同的文件名"""
        sim = calculate_filename_similarity("合同", "发票")
        assert sim < 0.5

    def test_similarity_empty(self):
        """空字符串"""
        assert calculate_filename_similarity("", "合同") == 0.0
        assert calculate_filename_similarity("合同", "") == 0.0


class TestMatchCache:
    """测试 MatchCache 类"""

    def test_cache_set_and_get(self):
        """缓存设置和获取"""
        cache = MatchCache(max_size=100, ttl_seconds=3600)
        
        cache.set("test.pdf", None, {"schema_id": "123", "confidence": 0.9})
        result = cache.get("test.pdf", None)
        
        assert result is not None
        assert result["schema_id"] == "123"
        assert result["confidence"] == 0.9

    def test_cache_miss(self):
        """缓存未命中"""
        cache = MatchCache(max_size=100, ttl_seconds=3600)
        
        result = cache.get("nonexistent.pdf", None)
        assert result is None

    def test_cache_lru_eviction(self):
        """LRU 淘汰机制"""
        cache = MatchCache(max_size=3, ttl_seconds=3600)
        
        cache.set("a.pdf", None, {"id": 1})
        cache.set("b.pdf", None, {"id": 2})
        cache.set("c.pdf", None, {"id": 3})
        cache.set("d.pdf", None, {"id": 4})  # 应淘汰 "a.pdf"
        
        assert cache.get("a.pdf", None) is None
        assert cache.get("d.pdf", None) is not None

    def test_cache_ttl_expiry(self):
        """TTL 过期"""
        import time
        cache = MatchCache(max_size=100, ttl_seconds=1)
        
        cache.set("test.pdf", None, {"schema_id": "123"})
        time.sleep(1.1)
        
        result = cache.get("test.pdf", None)
        assert result is None

    def test_cache_key_normalization(self):
        """缓存键标准化"""
        cache = MatchCache(max_size=100, ttl_seconds=3600)
        
        cache.set("合同2024001.pdf", None, {"schema_id": "123"})
        result = cache.get("合同2024002.pdf", None)  # 数字不同但标准化后相同
        
        assert result is not None

    def test_cache_clear(self):
        """清空缓存"""
        cache = MatchCache(max_size=100, ttl_seconds=3600)
        
        cache.set("a.pdf", None, {"id": 1})
        cache.set("b.pdf", None, {"id": 2})
        cache.clear()
        
        assert cache.get("a.pdf", None) is None
        assert cache.get("b.pdf", None) is None


class TestEdgeCases:
    """边界情况测试"""

    def test_special_characters_in_filename(self):
        """文件名含特殊字符"""
        result = normalize_filename("合同(2024)_V2.pdf")
        assert "(" not in result
        assert ")" not in result

    def test_chinese_filename(self):
        """中文文件名"""
        result = normalize_filename("货物采购合同.pdf")
        assert "货物采购合同" in result

    def test_very_long_chunk(self):
        """超长分块"""
        chunks = [{"page": 1, "text": "x" * 100000}]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks(chunks, max_chars=5000)
            assert len(result) <= 6000

    def test_unicode_in_chunks(self):
        """分块含 Unicode"""
        chunks = [{"page": 1, "text": "合同编号：CT-2024-001 金额：¥100,000.00"}]
        
        with patch('app.services.settings') as mock_settings:
            mock_settings.classify_max_chars = 5000
            mock_settings.classify_sample_strategy = "head_middle_tail"
            
            result = sample_text_from_chunks(chunks)
            assert "CT-2024-001" in result
            assert "100,000" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
