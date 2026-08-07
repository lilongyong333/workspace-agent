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

# 应用以非 root 运行：它会在容器内读写文件，
# 万一沙箱被绕过，非特权用户能显著限制影响范围。
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

# 这里**不写 USER appuser**。
#
# 持久卷挂载点的属主由平台决定（通常是 root），非特权进程写不进去 ——
# 而只有 root 能改它。所以容器以 root 启动，由 entrypoint 做一次 chown
# 之后**立刻 setuid 降权**再 exec 应用。
# 应用进程自始至终非特权，root 只存在于 entrypoint 的十几行里。
#
# 曾经在这里写死 USER appuser，结果挂卷之后建会话直接 500，
# 且报错完全看不出跟卷有关。

# Railway 通过 $PORT 注入端口，本地默认 8000
ENV PORT=8000
EXPOSE 8000

CMD ["python", "/app/entrypoint.py"]
