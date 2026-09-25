# VTranslate 服务端（计费 + 翻译中转 + 管理后台 /admin）
# 构建上下文为仓库根目录：docker build -f server/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/billing.db \
    HOST=0.0.0.0 \
    PORT=8765

WORKDIR /app
COPY requirements-server.txt .
RUN pip install -r requirements-server.txt

COPY server/*.py server/game_terms.json ./
COPY server/admin ./admin

RUN useradd -r -u 10001 vt && mkdir -p /data && chown vt /data
USER vt
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=4)" || exit 1

# 单进程运行（SQLite + 进程内状态），切勿加 --workers
# --proxy-headers 仅信任 FORWARDED_ALLOW_IPS（compose 中配置为反代所在网段）
CMD ["sh", "-c", "exec uvicorn app:app --host $HOST --port $PORT --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-127.0.0.1}\""]
