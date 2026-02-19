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

 # Create workdir root
 RUN mkdir -p /tmp/release-server-service
#
# # Non-root user
 RUN useradd --create-home appuser
 USER appuser
#
 ENV SERVICE_HOST=0.0.0.0
 ENV SERVICE_PORT=8080
 ENV LOG_LEVEL=INFO
 ENV LOCAL_WORKDIR_ROOT=/tmp/release-server-service
#
 EXPOSE 8080
#
 HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
     CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
#
     CMD ["uvicorn", "release_server_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
