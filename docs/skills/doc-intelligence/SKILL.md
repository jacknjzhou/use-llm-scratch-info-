---
name: "doc-intelligence"
description: "Calls the Doc Intelligence document extraction REST API (upload files, submit async LLM extraction tasks, poll status, export results). Invoke when the user asks to extract structured information from PDF/DOCX/Excel/images, or to manage extraction schemas of this project."
---

# Doc Intelligence 文档提取 Skill

通过 REST API 调用本项目部署的"智能信息提取系统"，完成：文件上传 → 提交异步提取任务 → 轮询状态 → 获取结构化结果/导出。

## 环境变量（调用前确认）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DOCINT_BASE_URL` | `http://localhost:8008` | 服务地址（nginx 入口） |
| `DOCINT_API_KEY` | 空 | 服务端设置了 `API_KEY` 时必填，以 `X-API-Key` 头传递 |
| `DOCINT_TIMEOUT` | `600` | 整个任务的最长等待秒数 |

服务未启动时先在项目根目录执行 `docker compose up -d`。

## 一键脚本（推荐）

`scripts/extract.sh` 封装了完整异步流程：上传 → 提交 → 轮询 → 输出结果 JSON（可选导出 xlsx）。

```bash
# 智能匹配模板（不指定 schema_id，系统自动为每个文件匹配）
scripts/extract.sh 报告.pdf 合同.docx

# 指定模板 + 结果保存到文件 + 同时导出 xlsx
scripts/extract.sh --schema <SCHEMA_ID> --out result.json --xlsx result.xlsx *.pdf
```

脚本退出码：`0` 成功；`1` 参数/网络/上传错误；`2` 任务失败或取消；`3` 部分成功（检查输出中各文件的 `status`）。

## API 速查（脚本未覆盖的场景直接用 curl）

所有请求需带认证头（若设置了 `API_KEY`）：`-H "X-API-Key: $DOCINT_API_KEY"`。

### 1. 列出提取模板

```bash
curl -s "$DOCINT_BASE_URL/api/schemas" | python3 -m json.tool
```

返回数组，关注 `id`、`name`、`fields`（字段定义 key/label/type）。用户说"用 XX 模板提取"时，先在此列表中按名称匹配出 `schema_id`。

### 2. 上传文件（多文件，表单字段名固定为 files）

```bash
curl -s -H "X-API-Key: $DOCINT_API_KEY" \
  -F "files=@/path/a.pdf" -F "files=@/path/b.docx" \
  "$DOCINT_BASE_URL/api/files"
```

返回 `[{"file_id": "...", "filename": "...", "size": 123}]`。支持格式：pdf/docx/xlsx/txt/csv/md/png/jpg/jpeg，单文件上限 50MB。

### 3. 提交提取任务

```bash
curl -s -H "X-API-Key: $DOCINT_API_KEY" -H "Content-Type: application/json" \
  -d '{"file_ids": ["<FILE_ID>"], "schema_id": "<SCHEMA_ID 或 null>", "priority": 5}' \
  "$DOCINT_BASE_URL/api/extract"
```

- `schema_id` 传 `null`（或省略）= 智能匹配；传具体 id = 指定模板
- `priority` 0~9，`>=8` 走加急队列
- 返回 `{"task_id": "..."}`，**必须保存 task_id 用于后续轮询**

### 4. 轮询任务状态（每 3~5 秒一次）

```bash
curl -s -H "X-API-Key: $DOCINT_API_KEY" "$DOCINT_BASE_URL/api/tasks/<TASK_ID>"
```

- 任务级 `status`：`pending` / `running`（带 `progress` 百分比）/ 终态 `succeeded` / `failed` / `partially_succeeded` / `cancelled`
- 终态后 `files` 数组含每个文件的提取结果：`results.<field_key>.value`（提取值）、`.evidence`（原文依据）、`.label`（字段名），以及 `coverage`（字段覆盖率）

### 5. 导出结果

```bash
# JSON
curl -s "$DOCINT_BASE_URL/api/tasks/<TASK_ID>/export?format=json"
# Excel
curl -s -o task_result.xlsx "$DOCINT_BASE_URL/api/tasks/<TASK_ID>/export?format=xlsx"
```

## 使用守则

1. **优先用 `scripts/extract.sh`**，仅在其不覆盖的操作（模板增删改、人工修正、取消任务）才手写 curl
2. 向用户报告结果时，按文件组织：文件名 → 模板 → 各字段值（附 evidence 便于核对）；`partially_succeeded` 时明确指出失败文件及 `error` 原因
3. 用户未指明模板时用智能匹配（省略 schema_id），并在结果中展示系统匹配到的 `schema_name` 与 `match_confidence`
4. 结果中 `corrected: true` 的字段是人工修正值，`original_value` 为模型原始输出，报告时以修正值为准
5. 不要向 `/api/llm-configs` 发起写操作（增删改 LLM 配置属管理行为），除非用户明确要求
