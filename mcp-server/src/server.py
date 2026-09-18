"""
MCP Server 主入口
提供 SSE 模式的 HTTP API 服务
"""
import asyncio
import json
import logging
import argparse
import base64
import os
from typing import Any, Dict

from .config import settings
from .tools.extract import extract_tool
from .security.auth import verify_api_key, AuthError
from .security.validator import PathValidationError
from .database import get_db_session, get_schema_by_name

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 工具定义
TOOLS = [
    {
        "name": "extract_document",
        "description": "提取文档内容并返回结构化数据。根据文件内容识别文档类型，然后提取对应字段信息。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（本地路径）"},
                "file_content": {"type": "string", "description": "Base64 编码的文件内容（与 file_path 二选一）"},
                "file_name": {"type": "string", "description": "文件名（使用 file_content 时必需）"},
                "schema_name": {"type": "string", "description": "指定模板名称（可选，不填则自动识别）"},
                "auto_classify": {"type": "boolean", "description": "是否自动识别文档类型", "default": True},
                "_api_key": {"type": "string", "description": "API 认证密钥"}
            }
        }
    },
    {
        "name": "classify_document",
        "description": "识别文档类型，返回匹配的模板信息（不提取内容）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
                "file_content": {"type": "string", "description": "Base64 编码的文件内容"},
                "file_name": {"type": "string", "description": "文件名"},
                "_api_key": {"type": "string", "description": "API 认证密钥"}
            }
        }
    },
    {
        "name": "list_templates",
        "description": "获取所有可用的文档模板列表",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "筛选特定类别的模板"},
                "_api_key": {"type": "string", "description": "API 认证密钥"}
            }
        }
    },
    {
        "name": "get_template",
        "description": "获取指定模板的详细信息",
        "inputSchema": {
            "type": "object",
            "properties": {
                "schema_name": {"type": "string", "description": "模板名称"},
                "_api_key": {"type": "string", "description": "API 认证密钥"}
            }
        }
    },
]

async def _classify_document(file_path: str = None, file_content: str = None, file_name: str = None) -> Dict[str, Any]:
    """文档分类"""
    from .tools.extract import _call_backend_api
    
    files = {}
    data = {"auto_classify": True}
    
    if file_path:
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                files["file"] = (os.path.basename(file_path), f.read())
        else:
            return {"success": False, "error": f"File not found: {file_path}"}
    elif file_content:
        content = base64.b64decode(file_content)
        files["file"] = (file_name or "document.pdf", content)
    else:
        return {"success": False, "error": "Either file_path or file_content is required"}
    
    return await _call_backend_api("POST", "/files", files=files, data=data)

async def handle_tool_call(name: str, arguments: dict) -> Dict[str, Any]:
    """处理工具调用"""
    try:
        logger.info(f"处理工具调用: {name}")
        
        # 认证检查
        api_key = arguments.pop("_api_key", None)
        if settings.api_key and api_key != settings.api_key:
            raise AuthError("Invalid API key")
        
        result = {}
        
        if name == "extract_document":
            result = await extract_tool(
                file_path=arguments.get("file_path"),
                file_content=arguments.get("file_content"),
                file_name=arguments.get("file_name"),
                schema_name=arguments.get("schema_name"),
                auto_classify=arguments.get("auto_classify", True),
                api_key=api_key
            )
        elif name == "classify_document":
            result = await _classify_document(
                file_path=arguments.get("file_path"),
                file_content=arguments.get("file_content"),
                file_name=arguments.get("file_name")
            )
        elif name == "list_templates":
            from .database import get_all_schemas
            async with get_db_session() as session:
                schemas = await get_all_schemas(session, arguments.get("category"))
                result = {
                    "success": True,
                    "data": [
                        {"id": str(s.id), "name": s.name, "category": s.category,
                         "description": s.description, "is_preset": s.is_preset}
                        for s in schemas
                    ],
                    "error": None
                }
        elif name == "get_template":
            schema_name = arguments.get("schema_name")
            async with get_db_session() as session:
                schema = await get_schema_by_name(session, schema_name)
                if schema:
                    result = {
                        "success": True,
                        "data": {
                            "id": str(schema.id), "name": schema.name,
                            "category": schema.category, "description": schema.description,
                            "fields": schema.fields or [], "is_preset": schema.is_preset
                        },
                        "error": None
                    }
                else:
                    result = {"success": False, "data": None, "error": f"Template not found: {schema_name}"}
        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}
        
        return result
        
    except PathValidationError as e:
        logger.warning(f"路径验证失败: {e}")
        return {"success": False, "error": f"Path validation error: {str(e)}"}
    except AuthError as e:
        logger.warning(f"认证失败: {e}")
        return {"success": False, "error": str(e)}
    except Exception as e:
        logger.error(f"处理工具调用失败: {e}", exc_info=True)
        return {"success": False, "error": f"Internal error: {str(e)}"}

async def run_sse(host: str = None, port: int = None):
    """运行 SSE 模式 (HTTP API)"""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from uvicorn import Config, Server
    
    host = host or settings.mcp_host
    port = port or settings.mcp_port
    
    app = FastAPI(title="Document Extract MCP Server")
    
    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "document-extract", "version": "1.0.0"}
    
    @app.get("/tools")
    async def list_tools():
        return {"tools": TOOLS}
    
    @app.post("/tools/{tool_name}")
    async def call_tool(tool_name: str, request: Request):
        body = await request.json()
        result = await handle_tool_call(tool_name, body)
        return JSONResponse(content={"result": [result]})
    
    @app.post("/extract")
    async def extract_document(request: Request):
        body = await request.json()
        result = await handle_tool_call("extract_document", body)
        return JSONResponse(content={"result": [result]})
    
    @app.post("/classify")
    async def classify_document(request: Request):
        body = await request.json()
        result = await handle_tool_call("classify_document", body)
        return JSONResponse(content={"result": [result]})
    
    @app.get("/templates")
    async def list_templates(category: str = None):
        result = await handle_tool_call("list_templates", {"category": category} if category else {})
        return JSONResponse(content={"result": [result]})
    
    @app.get("/template/{schema_name}")
    async def get_template(schema_name: str):
        result = await handle_tool_call("get_template", {"schema_name": schema_name})
        return JSONResponse(content={"result": [result]})
    
    logger.info(f"启动 MCP Server (SSE 模式) http://{host}:{port}")
    
    # 使用 Uvicorn Server 手动启动，避免 asyncio.run() 嵌套问题
    config = Config(app=app, host=host, port=port, log_level="info")
    server = Server(config)
    
    # 启动服务器
    await server.serve()

async def run_stdio():
    """运行 Stdio 模式 - MCP 协议"""
    logger.info("启动 MCP Server (Stdio 模式)")
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import Tool, TextContent
        
        server = Server("document-extract")
        
        @server.list_tools()
        async def list_tools_handler() -> list:
            return [Tool(**t) for t in TOOLS]
        
        @server.call_tool()
        async def call_tool_handler(name: str, arguments: dict) -> list:
            result = await handle_tool_call(name, arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(
                    server_name="document-extract",
                    server_version="1.0.0",
                )
            )
    except ImportError as e:
        logger.error(f"MCP SDK 导入失败: {e}")
        logger.info("请安装 mcp>=1.0.0 或使用 SSE 模式")
        raise

async def main():
    """主入口"""
    parser = argparse.ArgumentParser(description="Document Extract MCP Server")
    parser.add_argument("--mode", choices=["stdio", "sse"], default="sse", help="Server mode")
    parser.add_argument("--host", default=settings.mcp_host, help="SSE host")
    parser.add_argument("--port", type=int, default=settings.mcp_port, help="SSE port")
    
    args = parser.parse_args()
    
    if args.mode == "stdio":
        await run_stdio()
    else:
        await run_sse(args.host, args.port)

if __name__ == "__main__":
    asyncio.run(main())
