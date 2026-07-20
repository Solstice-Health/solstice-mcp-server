# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip install -r requirements.txt

RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --no-create-home --shell /usr/sbin/nologin mcp

COPY mcp_main.py .
COPY config/ config/
COPY src/ src/

USER mcp
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["gunicorn", "-w", "2", "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", "--timeout", "120", "mcp_main:app"]
