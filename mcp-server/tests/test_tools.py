"""
MCP Server 工具测试
"""
import pytest
import asyncio
from pathlib import Path
import tempfile
import os

# 添加 src 目录到路径
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp_server.src.tools.extract import ExtractTool
from mcp_server.src.security.validator import validate_path, validate_base64_data, PathValidationError


class TestExtractTool:
    """提取工具测试"""
    
    @pytest.fixture
    def tool(self):
        return ExtractTool()
    
    @pytest.fixture
    def temp_file(self):
        """创建临时测试文件"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write("这是一份普通发票测试文件\n发票代码：12345678\n金额：100.00元")
            temp_path = f.name
        
        yield temp_path
        
        # 清理
        if os.path.exists(temp_path):
            os.remove(temp_path)
    
    def test_parse_text_file(self, tool, temp_file):
        """测试文本文件解析"""
        result = asyncio.run(tool._parse_file(temp_file, {}))
        assert result is not None
        assert "普通发票" in result
    
    def test_validate_path_valid(self):
        """测试有效路径验证"""
        with tempfile.NamedTemporaryFile(suffix='.txt', delete=False) as f:
            f.write("test")
            path = f.name
        
        try:
            # 添加临时目录到允许列表
            from mcp_server.src.config import settings
            settings.allowed_paths = [str(Path(path).parent)]
            
            result = validate_path(path)
            assert result == path
        finally:
            os.remove(path)
    
    def test_validate_path_invalid(self):
        """测试无效路径"""
        with pytest.raises(PathValidationError):
            validate_path("/etc/passwd")
    
    def test_validate_base64_data(self):
        """测试 Base64 验证"""
        import base64
        
        # 有效数据
        data = base64.b64encode(b"hello world").decode()
        result = validate_base64_data(data)
        assert result == b"hello world"
        
        # 无效数据
        with pytest.raises(PathValidationError):
            validate_base64_data("not-valid-base64!!!")


class TestPathValidation:
    """路径验证测试"""
    
    def test_denied_paths(self):
        """测试禁止路径"""
        denied = ["/etc/passwd", "/var/log/syslog", "/root/.bashrc"]
        for path in denied:
            with pytest.raises(PathValidationError):
                validate_path(path)
    
    def test_empty_path(self):
        """测试空路径"""
        with pytest.raises(PathValidationError):
            validate_path("")
        
        with pytest.raises(PathValidationError):
            validate_path(None)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
