# Document Extract MCP Server

文档识别服务的 MCP（Model Context Protocol）扩展，允许外部应用通过标准化协议调用文档内容提取能力。

## 与 Backend 服务的交互

```
┌─────────────────────────────────────────────────────────────┐
│                    docker-compose                           │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────┐      ┌─────────────────────────────────┐   │
│  │  MCP Server │      │         Backend API             │   │
│  │             │      │                                 │   │
│  │  工具调用    │─────►│  POST /api/files (上传文件)    │   │
│  │             │      │  POST /api/extract (创建任务)   │   │
│  │             │◄─────│  GET  /api/tasks/{id}         │   │
│  │             │      │                                 │   │
│  │  工具返回    │      │  services.py (两阶段分类)     │   │
│  │  结构化数据  │      │  celery worker (异步处理)      │   │
│  └─────────────┘      └─────────────────────────────────┘   │
│         │                            │                     │
│         │  共享 PostgreSQL           │                     │
│         └────────────────────────────┘                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 交互流程

1. **用户调用 MCP 工具**（如 `extract_document`）
2. **MCP 上传文件** → `POST /api/files`
3. **MCP 创建任务** → `POST /api/extract`
4. **Backend 处理** → Celery Worker 执行两阶段分类（粗筛+精排）
5. **MCP 轮询任务状态** → `GET /api/tasks/{task_id}`
6. **返回结果** → 结构化的文档提取数据

### 复用 Backend 能力

| Backend 组件 | MCP 复用方式 |
|-------------|-------------|
| 两阶段分类 | API 调用，复用 `classify_file` 逻辑 |
| 字段提取 | API 调用，复用 `extract_fields` 逻辑 |
| LLM 配置 | Backend 管理，MCP 不需单独配置 |
| 任务队列 | Celery Worker 异步处理 |
| 数据库 | 共享 PostgreSQL，读取模板信息 |

## 启动方式

### 方式一：一键启动所有服务

```bash
# 启动所有服务（包括 MCP）
docker compose up --build -d

# 查看服务状态
docker compose ps
```

### 方式二：仅启动 MCP 服务

```bash
cd mcp-server
docker build -t doc-extract-mcp .
docker run --rm \
  --network doc-extract-network \
  -e DATABASE_URL="postgresql+asyncpg://extractor:extractor@postgres:5432/extractor" \
  -e API_BASE_URL="http://api:8000" \
  -v $(pwd)/data:/data:ro \
  doc-extract-mcp --mode stdio
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| DATABASE_URL | PostgreSQL 连接 | postgresql+asyncpg://... |
| API_BASE_URL | Backend API 地址 | http://api:8000 |
| API_KEY | API 认证密钥 | - |
| MCP_PORT | SSE 端口 | 8080 |
| MCP_FILE_PATH | 可访问文件路径 | ./data |

## API 接口

### MCP SSE 模式

```bash
# 提取文档
curl -X POST http://localhost:8080/tools/extract_document \
  -H "Content-Type: application/json" \
  -d '{
    "file_path": "/data/invoice.pdf",
    "auto_classify": true
  }'

# 分类文档
curl -X POST http://localhost:8080/tools/classify_document \
  -H "Content-Type: application/json" \
  -d '{"file_path": "/data/contract.pdf"}'

# 列出模板
curl http://localhost:8080/templates
```

## Claude Desktop 配置

```json
{
  "mcpServers": {
    "doc-extract": {
      "command": "docker",
      "args": [
        "run", "--rm",
        "--network", "doc-extract-network",
        "-e", "DATABASE_URL=postgresql+asyncpg://extractor:extractor@postgres:5432/extractor",
        "-e", "API_BASE_URL=http://api:8000",
        "-v", "/path/to/files:/data:ro",
        "doc-extract-mcp"
      ]
    }
  }
}
```

## 提供的工具

| 工具 | 功能 | Backend API |
|------|------|-------------|
| extract_document | 文档内容提取 | /api/extract |
| classify_document | 文档分类 | /api/extract |
| list_templates | 列出模板 | 直接查 DB |
| get_template | 获取模板详情 | 直接查 DB |

## 示例对话

**用户**：帮我提取 ~/data/invoice.pdf 的发票信息

**Claude**（自动调用工具）：
```
extract_document(
  file_path="/Users/me/data/invoice.pdf",
  auto_classify=true
)
```

**MCP 内部流程**：
```
1. 上传文件 → POST /api/files
2. 创建任务 → POST /api/extract
3. 轮询状态 → GET /api/tasks/{id}
4. 返回结果
```

**返回结果**：
```json
{
  "success": true,
  "data": {
    "schema_id": "uuid",
    "schema_name": "电子发票（普通发票）",
    "confidence": 0.95,
    "results": {
      "发票代码": {"value": "144031900110"},
      "发票号码": {"value": "12345678"},
      ...
    },
    "coverage": 0.85
  }
}
```

## 目录结构

```
project/
├── docker-compose.yml      # 统一的服务编排
├── backend/               # 主服务 Backend（提供 API）
├── frontend/              # 前端
├── mcp-server/            # MCP Server（调用 Backend API）
│   ├── src/
│   │   ├── server.py      # MCP 主入口
│   │   ├── config.py      # 配置（含 API_BASE_URL）
│   │   ├── database.py    # 数据库（读取模板）
│   │   └── tools/
│   │       └── extract.py # 提取工具（调用 Backend API）
│   └── Dockerfile
├── data/                  # MCP 可访问的文件目录
└── docs/
```

## 安全说明

1. **API 认证**：MCP 与 Backend 共享 API_KEY
2. **路径限制**：默认只允许访问 `/data`、`/tmp`、`/app/storage`
3. **任务隔离**：MCP 创建的任务与其他任务隔离处理
