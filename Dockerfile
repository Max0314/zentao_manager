FROM python:3.11-slim

# 避免生成 .pyc，并让日志直接输出到容器 stdout，方便 docker logs 查看。
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# 先安装依赖，再复制业务代码，利用 Docker 构建缓存提升重复构建速度。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

EXPOSE 8000

# 生产运行入口。FastAPI 应用对象位于 app/main.py 的 app 变量。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
