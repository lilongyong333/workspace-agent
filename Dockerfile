# 本地与线上跑同一个镜像 —— 这是可复现性的硬保证。
#
#   本地:  docker build -t workspace-agent . && docker run -p 8000:8000 --env-file .env workspace-agent
#   线上:  Railway 构建同一个 Dockerfile

FROM python:3.11-slim

# 不写 .pyc、日志不缓冲（容器里日志要立刻可见）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖单独一层：代码改动时不会重装依赖，构建快很多
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 以非 root 运行。这个应用会在容器内读写文件，
# 万一沙箱被绕过，非特权用户能显著限制影响范围。
#
# /data 必须**在切换用户之前**建好并授权：持久卷会挂到这里，
# 而挂载点默认属主是 root —— 非特权进程写不进去，
# 表现是建会话直接 500，且看不出跟"挂了个卷"有什么关系。
# 镜像里先建好同名目录并 chown，挂载时才有正确的属主可继承。
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

# Railway 通过 $PORT 注入端口，本地默认 8000
ENV PORT=8000
EXPOSE 8000

# 用 shell 形式以便展开 $PORT
CMD uvicorn web.app:app --host 0.0.0.0 --port ${PORT}
