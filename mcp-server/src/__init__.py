"""
Document Extract MCP Server
文档识别服务 MCP 扩展模块
"""
from .server import main, run_sse, run_stdio, handle_tool_call, TOOLS
from .config import settings

__all__ = ["main", "run_sse", "run_stdio", "handle_tool_call", "TOOLS", "settings"]
