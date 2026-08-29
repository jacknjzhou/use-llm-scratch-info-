# 智能信息提取系统（Doc Intelligence）

上传文件 → 自定义/预置关键词模板 → 大模型批量提取结构化信息（含原文出处核对）。

## 功能特性

- **关键词模板**：按文件类型分类，8 套出厂预置模板（合同/租赁合同/补充说明/行程单/住宿单/餐饮流水单/订购单/付款截图），可复制修改，字段勾选复用，版本留痕
- **多文件批量**：一次上传多个文件，文件级失败隔离，进度实时可查
- **双模式模板决策**：指定模板直接用；未指定时 LLM 读取文件前两页智能匹配模板（置信度三级决策 + 手动改派兜底）
- **OCR 识别**：png/jpg 图片直接 OCR（tesseract chi_sim+eng，容器内置）；扫描版 PDF 无文本层时自动逐页 OCR 兜底，低置信自动升 DPI 重试
- **防幻觉**：找不到的字段显式返回 null 并给出未找到状态，每个提取值附 evidence 原文片段与页码
- **人工修正回流**：结果页逐字段修正，修正记录自动成为该模板后续提取的 few-shot 参考（越用越准）
- **实时进度**：SSE 推送任务状态，无需轮询；结果可导出 Excel / JSON
- **模板共享**：模板 JSON 导入/导出，团队间一键复用
- **优先级与限流**：任务分加急/普通/低三档（双队列，加急优先消费）；LLM_MAX_RPS 可限制每秒调用频次
- **可插拔存储**：STORAGE_BACKEND 切换本地卷 / S3（兼容 OSS、MinIO）
- **LLM 自定义**：任意 OpenAI 兼容接口（GLM/DeepSeek/Ollama/vLLM…），界面配置 base_url/key/model，key 加密落库，保存前连通测试
- **生产化底座**：PostgreSQL（JSONB 存结果）+ Celery/Redis 异步队列 + Docker Compose 一键部署，worker 可横向扩容

## 快速开始

```bash
cp .env.example .env        # 修改 LLM_MASTER_KEY 为随机长字符串
docker compose up -d --build
# 打开 http://localhost:8008
```

首次使用：进入「模型配置」→ 填入 base_url / api_key / model → 测试连通 → 设为默认 → 发起任务。

## 目录结构

```
backend/            FastAPI + Celery Worker
  app/models.py         六张核心表 + 人工修正表（模板/文件/任务/任务文件/结果/LLM配置/修正）
  app/parsers.py        docx/pdf/xlsx/txt 解析与分块；图片与扫描页 OCR（PyMuPDF + tesseract）
  app/storage.py        存储抽象：local / s3（兼容 OSS、MinIO）
  app/llm_client.py     OpenAI 兼容异步客户端（JSON Mode 自动降级，可选 RPS 限流）
  app/services.py       智能模板匹配 + chunk 并发提取 + few-shot 修正回流
  app/tasks/            Celery 任务编排（文件级失败隔离，双队列优先级）
  app/routers/          files / schemas / llm_configs / tasks（导出/SSE/修正/改派）
  seeds/                8 套预置模板（首次启动自动写入）
frontend/           Vue3 单页应用（nginx 托管并反代 /api）
docker-compose.yml  api / worker / postgres / redis / frontend
```

## 关键设计

| 主题 | 说明 |
|---|---|
| 模板决策 | 指定模板 assigned / 智能匹配 auto；置信度 ≥0.8 自动绑定，0.5~0.8 待确认，<0.5 未匹配可手动改派 |
| OCR | tesseract chi_sim+eng（镜像内置）；PDF 文本页直读、空白页自动 OCR；OCR_LANG 可在 .env 覆盖 |
| 防幻觉 | prompt 强约束"找不到填 null 禁止编造"；JSON 校验失败自动重试；response_format 不支持时自动降级 |
| 并发 | Celery prefork 多进程（文件级）+ Worker 内 asyncio 信号量（chunk 级 LLM 调用） |
| 可靠性 | 任务状态落 PG；单文件失败不影响任务内其他文件；任务级部分成功状态 |
| 溯源 | schema_snapshot 保存任务发起时的模板快照；match_reason 记录智能匹配理由 |

## 扩容

```bash
docker compose up -d --scale worker=4
```

## API

启动后访问 http://localhost:8008/api/docs 查看交互式文档。
