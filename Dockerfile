FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AIRELAY_DATA_DIR=/data

WORKDIR /app
COPY *.py ./
COPY static/ ./static/
COPY bin/ ./bin/
RUN mkdir -p /data && chown 10001:10001 /data
USER 10001:10001

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=3); assert json.load(r)['ok']"]

ENTRYPOINT ["python", "/app/relay.py"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8787", "--public-url", "http://localhost:8787"]
