# MCP 服务扩展方案

## 概述

本文档描述了文档识别服务如何通过 MCP (Model Context Protocol) 协议扩展，为其他应用提供文档内容识别的能力。

## 服务架构

```
┌─────────────────┐     HTTP/SSE      ┌─────────────────┐
│   MCP Client    │ ◄───────────────► │   MCP Server    │
│ (Claude Desktop)│                   │   (SSE 模式)     │
└─────────────────┘                   └────────┬────────┘
                                              │
                                              │ HTTP API
                                              ▼
                                       ┌─────────────────┐
                                       │   Backend API   │
                                       │   (端口 8000)   │
                                       └────────┬────────┘
                                              │
                                              ▼
                                       ┌─────────────────┐
                                       │  PostgreSQL DB  │
                                       └─────────────────┘
```

## 快速开始

### 1. 启动服务

使用 docker compose 启动所有服务（包括 MCP 服务）：

```bash
# 构建并启动所有服务
docker compose up --build -d

# 仅启动 MCP SSE 服务
docker compose up -d mcp-sse
```

### 2. 验证服务状态

```bash
# 检查 MCP 服务健康状态
curl http://localhost:8080/health

# 查看可用工具列表
curl http://localhost:8080/tools
```

## API 接口

### 健康检查

```
GET /health
```

响应示例：
```json
{
  "status": "ok",
  "service": "document-extract",
  "version": "1.0.0"
}
```

### 工具列表

```
GET /tools
```

响应示例：
```json
{
  "tools": [
    {
      "name": "extract_document",
      "description": "提取文档内容并返回结构化数据",
      "inputSchema": {...}
    },
    ...
  ]
}
```

### 文档提取

```
POST /extract
Content-Type: application/json

{
  "file_path": "/data/invoice.pdf",
  "schema_name": "增值税发票",
  "auto_classify": true,
  "_api_key": "your-api-key"
}
```

或使用 Base64 编码的文件内容：

```json
{
  "file_content": "base64-encoded-content...",
  "file_name": "invoice.pdf",
  "auto_classify": true
}
```

### 文档分类

```
POST /classify
Content-Type: application/json

{
  "file_path": "/data/document.pdf"
}
```

### 模板列表

```
GET /templates?category=发票
```

### 获取模板详情

```
GET /template/{schema_name}
```

## MCP 工具定义

### extract_document

提取文档内容并返回结构化数据。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 否 | 文件路径（与 file_content 二选一） |
| file_content | string | 否 | Base64 编码的文件内容 |
| file_name | string | 否 | 文件名（使用 file_content 时必需） |
| schema_name | string | 否 | 指定模板名称，不填则自动识别 |
| auto_classify | boolean | 否 | 是否自动识别文档类型，默认 true |
| _api_key | string | 否 | API 认证密钥 |

### classify_document

识别文档类型，返回匹配的模板信息（不提取内容）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_path | string | 否 | 文件路径 |
| file_content | string | 否 | Base64 编码的文件内容 |
| file_name | string | 否 | 文件名 |
| _api_key | string | 否 | API 认证密钥 |

### list_templates

获取所有可用的文档模板列表。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| category | string | 否 | 筛选特定类别的模板 |
| _api_key | string | 否 | API 认证密钥 |

### get_template

获取指定模板的详细信息。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| schema_name | string | 是 | 模板名称 |
| _api_key | string | 否 | API 认证密钥 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| DATABASE_URL | postgresql+asyncpg://... | 数据库连接字符串 |
| API_BASE_URL | http://api:8000 | Backend API 地址 |
| MCP_HOST | 0.0.0.0 | MCP 服务监听地址 |
| MCP_PORT | 8080 | MCP 服务监听端口 |
| API_KEY | - | API 认证密钥 |
| MCP_FILE_PATH | ./data | 挂载的本地文件目录 |

## Docker Compose 配置

```yaml
mcp-sse:
  build: ./mcp-server
  container_name: doc-mcp-sse
  restart: unless-stopped
  command: --mode sse --host 0.0.0.0 --port 8080
  environment:
    DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-extractor}:${POSTGRES_PASSWORD:-extractor}@postgres:5432/${POSTGRES_DB:-extractor}
    API_BASE_URL: http://api:8000
    API_KEY: ${API_KEY:-}
  ports:
    - "${MCP_PORT:-8080}:8080"
  volumes:
    - filedata:/app/storage
    - ${MCP_FILE_PATH:-./data}:/data:ro
  depends_on:
    postgres:
      condition: service_healthy
    api:
      condition: service_started
  networks:
    - doc-network
```

## 使用示例

### Python 调用示例

```python
import requests
import base64

# 读取文件并提取内容
with open("invoice.pdf", "rb") as f:
    file_content = base64.b64encode(f.read()).decode()

response = requests.post("http://localhost:8080/extract", json={
    "file_content": file_content,
    "file_name": "invoice.pdf",
    "auto_classify": True
})

result = response.json()
print(result["result"][0])
```

### cURL 调用示例

```bash
# 提取文档
curl -X POST http://localhost:8080/extract \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "/data/invoice.pdf",
    "auto_classify": true
  }'

# 分类文档
curl -X POST http://localhost:8080/classify \
  -H "Content-Type: application/json" \
  -d '{"file_path": "/data/document.pdf"}'

# 获取模板列表
curl http://localhost:8080/templates

# 获取特定模板
curl http://localhost:8080/template/增值税发票
```

## 与 Claude Desktop 集成

在 Claude Desktop 配置文件中添加：

```json
{
  "mcpServers": {
    "document-extract": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "--network=doc-extract-network",
        "-v", "/path/to/docs:/data:ro",
        "use-llm-scratch-info--mcp-sse",
        "--mode", "stdio"
      ]
    }
  }
}
```

## 错误处理

所有接口返回统一格式的错误信息：

```json
{
  "result": [{
    "success": false,
    "error": "错误描述"
  }]
}
```

常见错误：

| 错误代码 | 说明 |
|----------|------|
| File not found | 指定路径的文件不存在 |
| Invalid API key | API 密钥验证失败 |
| Template not found | 指定的模板不存在 |
| Internal error | 服务器内部错误 |

## 性能优化

- MCP 服务与 Backend API 使用内部网络通信
- 文件通过挂载卷共享，避免重复传输
- 支持并发处理多个文档
