# Release Server Service

A standalone, containerized REST API service for managing Cerebras inference server replicas.

## Overview

This service extracts the server lifecycle management from `test_release_server.py::test_start_server`
into an independent REST API. It handles:

1. **Creating** inference server replicas (FastAPI replica, API Gateway, Platform Workload)
2. **Tracking** multiple concurrent replicas with full status lifecycle
3. **Health checking** replicas
4. **Stopping** replicas cleanly

### Key Design Principle

**The service is independent of any monolith branch.** All branch-specific dependencies
(model configuration, replica config, API config, platform config) are provided via the
REST request payload. The service itself only depends on the inference server controller
package (`cerebras.regress.common.integration.cif.inference_server_ctl`) which is installed
in the container image.

## Supported Server Modes

| Mode | Description |
|------|-------------|
| `replica` | Full FastAPI replica server on real hardware |
| `replica_mock` | FastAPI replica with mock backend |
| `api_gateway` | API Gateway server |
| `api_gateway_mock` | API Gateway with mock backend |
| `platform_workload` | Platform Workload server |
| `platform_workload_mock` | Platform Workload with mock backend |

## Quick Start

### Docker

```bash
docker build -t release-server-service .
docker run -p 8080:8080 release-server-service
```

### Docker Compose

```bash
docker-compose up -d
```

### Direct

```bash
pip install -e .
uvicorn release_server_service.main:app --host 0.0.0.0 --port 8080
```

## API Reference

### Create a Replica

```bash
POST /api/v1/replicas
```

```json
{
  "server_mode": "replica",
  "model_name": "llama3.1-8b",
  "full_config": {
    "model": { "pipeline": { "num_csx": 1 } },
    "runconfig": { "job_labels": [], "job_priority": "p2" },
    "api_config": {}
  },
  "placement": {
    "multibox": "dh1",
    "usernode": "usernode-01",
    "namespace": "inf-integ",
    "app_tag": "my-app"
  },
  "replica_config": {
    "replica_config": { "max_batch_size": 32 },
    "api_config": { "timeout": 120 }
  },
  "job": {
    "job_priority": "p2",
    "job_timeout_s": 86400
  },
  "wait_for_ready": true,
  "run_diagnostics": true
}
```

**Response (201):**
```json
{
  "replica_id": "a1b2c3d4e5f6",
  "status": "ready",
  "message": "Replica a1b2c3d4e5f6 created successfully",
  "base_url": "http://host:port"
}
```

### List Replicas

```bash
GET /api/v1/replicas
GET /api/v1/replicas?server_mode=replica&status=ready&model_name=llama3.1-8b
```

### Get Replica Status

```bash
GET /api/v1/replicas/{replica_id}
```

### Stop a Replica

```bash
POST /api/v1/replicas/{replica_id}/stop
```

### Health Check a Replica

```bash
POST /api/v1/replicas/{replica_id}/health
```

```json
{
  "timeout_s": 120,
  "poll_interval_s": 5
}
```

### Service Health

```bash
GET /health
```

## HTTP Status Codes

| Code
