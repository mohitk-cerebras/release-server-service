# ----  stage 1: Builder ───
FROM python:3.10-slim AS builder

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install .

# ─── Stage 2: Runtime ───
 FROM python:3.10-slim AS runtime

 WORKDIR /app

 # Copy installed packages from builder
 COPY --from=builder /install /usr/local

 # Copy source for reference
 COPY src/ src/

 # Non-root user
 RUN useradd --create-home appuser

 # Create workdir root and grant ownership to appuser
 RUN mkdir -p /n0/lab/test/dh1/ && chown -R appuser:appuser /n0/lab/test/dh1/

 USER appuser
#
 ENV SERVICE_HOST=0.0.0.0
 ENV SERVICE_PORT=8080
 ENV LOG_LEVEL=INFO
 ENV LOCAL_WORKDIR_ROOT=/n0/lab/test/dh1/
#
 EXPOSE 8080
#
 HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
     CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
#
     CMD ["uvicorn", "release_server_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
