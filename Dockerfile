FROM python:3.12-slim

LABEL org.opencontainers.image.title="Cloud Ops Agent" \
      org.opencontainers.image.description="Multi-Agent SRE & Cloud Operations Mission Control with MCP Servers, Guardrails, and Multimodal Reporting" \
      org.opencontainers.image.version="1.0.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    CLOUD_OPS_HOST=0.0.0.0 \
    CLOUD_OPS_PORT=8000 \
    CLOUD_OPS_ARTIFACTS_DIR=/app/artifacts \
    CLOUD_OPS_DATA_DIR=/app/data

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tests/ ./tests/

RUN mkdir -p /app/artifacts /app/data && \
    pip install --no-cache-dir -e .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "cloud_ops", "serve", "--host", "0.0.0.0", "--port", "8000"]
