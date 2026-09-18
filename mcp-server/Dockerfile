FROM python:3.12-slim

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libmagic1 \
    poppler-utils \
    tesseract-ocr \
    tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 创建包目录并复制源代码（将 src 内容复制到 mcp_server 目录）
RUN mkdir -p /app/mcp_server
COPY src/ /app/mcp_server/

# 创建存储目录
RUN mkdir -p /app/storage

# 环境变量
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 入口点 - 支持 stdio 和 sse 模式
ENTRYPOINT ["python", "-m", "mcp_server"]
CMD ["--mode", "stdio"]
