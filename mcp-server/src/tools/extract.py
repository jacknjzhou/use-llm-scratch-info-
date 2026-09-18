"""
MCP Server 文档提取工具
复用 Backend 的 API 进行文档分类和提取
"""
import os
import uuid
import json
import asyncio
import logging
from typing import Optional, Dict, Any, List

import aiohttp
from ..config import settings
from ..security.validator import validate_path, validate_base64_data, ALLOWED_SUFFIXES
from ..database import get_db_session, get_all_schemas, get_schema_by_name, get_schema_by_id

logger = logging.getLogger(__name__)


class ExtractTool:
    """文档提取工具 - 复用 Backend API"""
    
    def __init__(self):
        self.api_base_url = os.getenv("API_BASE_URL", "http://api:8000")
        self.api_key = settings.api_key
    
    def _get_headers(self) -> Dict[str, str]:
        """获取请求头"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers
    
    async def extract_document(
        self,
        file_path: Optional[str] = None,
        file_content: Optional[str] = None,
        file_name: Optional[str] = None,
        schema_name: Optional[str] = None,
        auto_classify: bool = True,
        options: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        提取文档内容 - 通过 Backend API
        """
        options = options or {}
        temp_path = None
        
        try:
            # 1. 获取文件内容
            if file_content:
                file_bytes = validate_base64_data(
                    file_content, 
                    max_size_mb=options.get("max_size_mb", settings.max_file_size_mb)
                )
                file_name = file_name or "document"
                temp_path = await self._save_temp_file(file_bytes, file_name)
                file_path = temp_path
            elif file_path:
                file_path = validate_path(file_path, ALLOWED_SUFFIXES)
            else:
                return self._error_result("必须提供 file_path 或 file_content")
            
            # 2. 调用 Backend API 创建任务
            task_result = await self._create_extract_task(file_path, schema_name)
            
            if not task_result.get("task_id"):
                return task_result
            
            task_id = task_result["task_id"]
            
            # 3. 等待任务完成
            final_result = await self._wait_task_complete(task_id)
            
            return final_result
            
        except Exception as e:
            logger.error(f"提取文档失败: {e}", exc_info=True)
            return self._error_result(str(e))
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except:
                    pass
    
    async def classify_document(
        self,
        file_path: Optional[str] = None,
        file_content: Optional[str] = None,
        file_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        识别文档类型 - 通过 Backend API
        """
        temp_path = None
        
        try:
            # 获取文件
            if file_content:
                file_bytes = validate_base64_data(file_content)
                file_name = file_name or "document"
                temp_path = await self._save_temp_file(file_bytes, file_name)
                file_path = temp_path
            elif file_path:
                file_path = validate_path(file_path, ALLOWED_SUFFIXES)
            else:
                return self._error_result("必须提供 file_path 或 file_content")
            
            # 调用分类 API
            result = await self._classify_via_api(file_path)
            
            return result
            
        except Exception as e:
            logger.error(f"分类文档失败: {e}", exc_info=True)
            return self._error_result(str(e))
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except:
                    pass
    
    async def list_templates(
        self,
        category: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        列出可用模板 - 直接查数据库
        """
        try:
            async with get_db_session() as session:
                schemas = await get_all_schemas(session, category)
            
            return {
                "success": True,
                "data": [
                    {
                        "id": str(s.id),
                        "name": s.name,
                        "category": s.category,
                        "field_count": len(s.fields) if s.fields else 0,
                        "is_preset": s.is_preset,
                        "description": s.description
                    }
                    for s in schemas
                ],
                "error": None
            }
            
        except Exception as e:
            logger.error(f"获取模板列表失败: {e}", exc_info=True)
            return self._error_result(str(e))
    
    # ==================== 内部方法 ====================
    
    async def _save_temp_file(self, content: bytes, filename: str) -> str:
        """保存临时文件"""
        import tempfile
        os.makedirs(settings.storage_dir, exist_ok=True)
        ext = os.path.splitext(filename)[1]
        temp_path = os.path.join(settings.storage_dir, f"mcp_{uuid.uuid4().hex}{ext}")
        
        with open(temp_path, "wb") as f:
            f.write(content)
        
        return temp_path
    
    async def _upload_file(self, file_path: str) -> Optional[str]:
        """
        上传文件到 Backend
        
        Returns:
            file_id 如果成功
        """
        filename = os.path.basename(file_path)
        
        try:
            with open(file_path, "rb") as f:
                file_content = f.read()
            
            import base64
            files = {
                "file": (filename, file_content)
            }
            data = aiohttp.FormData()
            data.add_field("file",
                          file_content,
                          filename=filename,
                          content_type=self._get_content_type(filename))
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.api_base_url}/api/files",
                    headers=self._get_headers(),
                    data=data
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return result.get("file_id")
                    else:
                        logger.warning(f"上传文件失败: {resp.status}")
                        return None
            
        except Exception as e:
            logger.error(f"上传文件失败: {e}")
            return None
    
    def _get_content_type(self, filename: str) -> str:
        """获取文件 MIME 类型"""
        ext = os.path.splitext(filename)[1].lower()
        types = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".csv": "text/csv",
            ".txt": "text/plain",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
        }
        return types.get(ext, "application/octet-stream")
    
    async def _create_extract_task(
        self, 
        file_path: str, 
        schema_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        创建提取任务
        """
        try:
            # 1. 上传文件
            file_id = await self._upload_file(file_path)
            if not file_id:
                return self._error_result("文件上传失败")
            
            # 2. 创建任务
            async with aiohttp.ClientSession() as session:
                payload = {
                    "file_ids": [file_id],
                    "auto_match": True
                }
                
                async with session.post(
                    f"{self.api_base_url}/api/extract",
                    headers=self._get_headers(),
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        return {
                            "success": True,
                            "task_id": result.get("task_id"),
                            "file_id": file_id
                        }
                    else:
                        text = await resp.text()
                        return self._error_result(f"创建任务失败: {resp.status} - {text}")
            
        except Exception as e:
            logger.error(f"创建提取任务失败: {e}")
            return self._error_result(str(e))
    
    async def _wait_task_complete(self, task_id: str, timeout: int = 120) -> Dict[str, Any]:
        """
        等待任务完成并获取结果
        """
        import time
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                async with aiohttp.ClientSession() as session:
                    # 获取任务状态
                    async with session.get(
                        f"{self.api_base_url}/api/tasks/{task_id}",
                        headers=self._get_headers(),
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            task = await resp.json()
                            status = task.get("status")
                            
                            if status == "succeeded":
                                # 获取详细结果
                                return await self._get_task_result(task_id)
                            elif status in ["failed", "cancelled"]:
                                return self._error_result(f"任务失败: {status}")
                            
                            # 继续等待
                            await asyncio.sleep(2)
                        else:
                            await asyncio.sleep(2)
                            
            except asyncio.TimeoutError:
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"查询任务状态失败: {e}")
                await asyncio.sleep(2)
        
        return self._error_result("任务超时")
    
    async def _get_task_result(self, task_id: str) -> Dict[str, Any]:
        """获取任务结果"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.api_base_url}/api/tasks/{task_id}",
                    headers=self._get_headers(),
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status == 200:
                        task = await resp.json()
                        
                        # 提取文件结果
                        files = task.get("files", [])
                        if files:
                            file_result = files[0]
                            return {
                                "success": True,
                                "data": {
                                    "schema_id": file_result.get("schema_id"),
                                    "schema_name": file_result.get("schema_name"),
                                    "confidence": file_result.get("confidence", 0),
                                    "match_status": file_result.get("match_status"),
                                    "results": file_result.get("results", {}),
                                    "coverage": file_result.get("coverage")
                                },
                                "error": None
                            }
                        
                        return self._error_result("任务无结果")
                    
                    return self._error_result(f"获取结果失败: {resp.status}")
                    
        except Exception as e:
            logger.error(f"获取任务结果失败: {e}")
            return self._error_result(str(e))
    
    async def _classify_via_api(self, file_path: str) -> Dict[str, Any]:
        """
        通过 API 进行文档分类
        """
        try:
            # 上传文件
            file_id = await self._upload_file(file_path)
            if not file_id:
                return self._error_result("文件上传失败")
            
            # 创建任务并等待完成
            task_result = await self._create_extract_task(file_path)
            if not task_result.get("task_id"):
                return task_result
            
            task_id = task_result["task_id"]
            result = await self._wait_task_complete(task_id, timeout=60)
            
            if result.get("success"):
                data = result.get("data", {})
                return {
                    "success": True,
                    "data": {
                        "schema_id": data.get("schema_id"),
                        "schema_name": data.get("schema_name"),
                        "confidence": data.get("confidence"),
                        "match_status": data.get("match_status")
                    },
                    "error": None
                }
            
            return result
            
        except Exception as e:
            logger.error(f"API 分类失败: {e}")
            return self._error_result(str(e))
    
    def _error_result(self, message: str) -> Dict[str, Any]:
        """错误结果"""
        return {
            "success": False,
            "data": None,
            "error": message
        }


# 全局工具实例
extract_tool = ExtractTool()
