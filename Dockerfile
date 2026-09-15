FROM python:3.12-slim

# Standard library only -- no pip install step, nothing to go stale.
WORKDIR /app
COPY server.py /app/server.py
COPY web /app/web

ENV HUB_PORT=8787 \
    HUB_DB=/data/hub.db \
    HUB_TOKENS_FILE=/data/tokens.json \
    HUB_WEB_DIR=/app/web \
    HUB_RETAIN_DAYS=14 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ['HUB_PORT']+'/health').read()"

CMD ["python", "/app/server.py"]
