FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DISCOVER_DATABASE_URL=sqlite:////data/discover.db \
    DISCOVER_CONFIG_PATH=/app/config/profiles.yaml \
    DISCOVER_IMAGES_DIR=/data/images

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install .

COPY config ./config

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/health').status != 200)"

# Open SSE streams would otherwise keep a stopping container alive until it is killed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
