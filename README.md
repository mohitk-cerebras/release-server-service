# Release Server Service

A standalone REST API service for managing Cerebras inference server replicas with true detached mode - replicas and wsjobs continue running even if the service restarts.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [API Reference](#api-reference)
- [Configuration](#configuration)
- [Detached Mode](#detached-mode)
- [State Management](#state-management)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## Overview

Release Server Service is a production-ready REST API for managing Cerebras inference server replicas. It provides:

- **Worker Process Architecture**: Each replica runs in an independent worker process
- **True Detached Mode**: Replicas and wsjobs survive service restarts
- **State Persistence**: Replica state stored in JSON files for crash recovery
- **Wsjob Tracking**: Monitors compile and execute jobs on the Cerebras cluster
- **External Connectivity**: Automatic FQDN resolution for remote client access
- **Health Monitoring**: Background monitoring of replica health and wsjob status

### Design Principles

1. **Independent Processes**: REST server, worker processes, and cluster jobs are independent
2. **No Direct Coupling**: Service doesn't depend on any specific monolith branch
3. **State Persistence**: All state survives restarts via file-based storage
4. **Fault Tolerance**: Service restarts don't affect running workloads

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     REST API Service (FastAPI)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  API Routes                                              │   │
│  │  • POST /api/v1/replicas      - Create replica          │   │
│  │  • GET  /api/v1/replicas      - List replicas           │   │
│  │  • GET  /api/v1/replicas/{id} - Get replica status      │   │
│  │  • DELETE /api/v1/replicas/{id} - Stop replica          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                            ↓                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  ReplicaManagerV2 (Orchestrator)                        │   │
│  │  • Creates worker processes                              │   │
│  │  • Reads/writes state files                             │   │
│  │  • Monitors replica health                              │   │
│  │  • Tracks wsjob IDs                                     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                            ↓                                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  State Manager (File-based Persistence)                 │   │
│  │  • Stores replica state as JSON files                   │   │
│  │  • Survives REST server restarts                        │   │
│  │  • Location: {workdir_root}/state/*.json               │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                            ↓ spawn (detached)
┌─────────────────────────────────────────────────────────────────┐
│                  Worker Processes (Independent)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  replica_worker.py (per replica)                        │   │
│  │  • Deploys cbclient environment (venv)                  │   │
│  │  • Installs packages from devpi                         │   │
│  │  • Creates inference server subprocess                  │   │
│  │  • Monitors health and updates state                   │   │
│  │  • Reads wsjob IDs from run_meta.json                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│                            ↓ spawn                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Inference Server (subprocess)                          │   │
│  │  • Runs appliance_host_inference.py                     │   │
│  │  • Listens on 127.0.0.1:{random_port}                   │   │
│  │  • Creates wsjobs on Cerebras cluster                   │   │
│  │  • Writes run_meta.json with wsjob IDs                  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                            ↓ submit jobs
┌─────────────────────────────────────────────────────────────────┐
│              Cerebras Cluster (Kubernetes Jobs)                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Compile Wsjobs (Kubernetes pods)                       │   │
│  │  • wsjob-{id}-coordinator-0                             │   │
│  │  • wsjob-{id}-worker-0                                  │   │
│  │  • Docker image: cbcore:{app_tag}                       │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Execute Wsjobs (Kubernetes pods)                       │   │
│  │  • wsjob-{id}-coordinator-0                             │   │
│  │  • wsjob-{id}-worker-0                                  │   │
│  │  • Runs inference on Cerebras hardware                  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

Key Characteristics:
• Worker processes: Spawned with start_new_session=True (detached)
• Wsjobs: Kubernetes jobs on cluster (completely independent)
• State: Persisted in JSON files (survives all restarts)
• Lifecycle: REST server ⊄ Workers ⊄ Wsjobs (all independent)
```

### Data Flow

```
1. User → REST API: POST /api/v1/replicas
                ↓
2. REST API → ReplicaManagerV2: create_replica(request)
                ↓
3. ReplicaManagerV2 → File System: Write request.json
                ↓
4. ReplicaManagerV2 → OS: Spawn worker (detached)
                ↓
5. Worker → venv: Deploy cbclient environment
                ↓
6. Worker → devpi: Install packages (pip)
                ↓
7. Worker → subprocess: Start inference server
                ↓
8. Inference Server → Cluster: Submit compile wsjob
                ↓
9. Inference Server → File: Write run_meta.json (wsjob IDs)
                ↓
10. Monitor → File: Read run_meta.json
                ↓
11. Monitor → State: Update compile_wsjob, execute_wsjob
                ↓
12. User → REST API: GET /api/v1/replicas/{id}
                ↓
13. REST API → State: Read replica state
                ↓
14. REST API → User: Return replica info (with wsjob IDs)
```

## Key Features

### 1. True Detached Mode
- **Replicas survive service restarts**: Workers run in independent processes
- **Wsjobs survive service restarts**: Cluster jobs independent of REST server
- **No work loss**: Long compilations (hours) continue even if service crashes
- **Fault tolerance**: Can upgrade/restart service without disrupting workloads

### 2. State Persistence
- **File-based state**: Replica state stored as JSON files in `{workdir_root}/state/`
- **Crash recovery**: Service can restart and reconnect to running replicas
- **No in-memory state**: All state persisted to disk immediately

### 3. Wsjob Tracking
- **Automatic detection**: Reads wsjob IDs from `run_meta.json`
- **Separate tracking**: Compile and execute jobs tracked independently
- **Timeline aware**: Handles jobs appearing at different times
- **API exposure**: Wsjob IDs returned in replica responses

### 4. External Connectivity
- **FQDN resolution**: Automatic conversion of 127.0.0.1 to host FQDN
- **Remote access**: Endpoints work from any machine
- **Format**: `http://net004-us-sr04.sck2.cerebrascloud.com:{port}`

### 5. Background Monitoring
- **Health checks**: Monitors replica process liveness
- **Wsjob updates**: Polls for new compile/execute jobs
- **Dead detection**: Automatically marks failed replicas
- **Interval**: Runs every 30 seconds

### 6. Package Management
- **Devpi integration**: Installs packages from internal devpi server
- **Traditional pip**: Uses pip (not uv) for reliable devpi resolution
- **Version conversion**: Converts app_tag to PEP 440 format
- **Docker tags**: Converts PEP 440 back to Docker-compatible format

## Quick Start

### Prerequisites

- Python 3.11+
- Access to Cerebras usernode
- Access to devpi server (`https://devpi.cerebras.aws/root/main/+simple/`)
- Access to Cerebras cluster (for wsjobs)

### Installation

#### Option 1: Direct Installation
```bash
# Clone repository
git clone <repo-url>
cd release-server-service

# Install dependencies
pip install -e .

# Run service
uvicorn release_server_service.main:app --host 0.0.0.0 --port 8000
```

#### Option 2: Docker
```bash
docker build -t release-server-service .
docker compose up -d
```

### Create Your First Replica

```bash
# Create replica
curl -X POST http://net004-us-sr04.sck2.cerebrascloud.com:8000/api/v1/replicas \
  -H "Content-Type: application/json" \
  -d '{
  "server_mode": "replica",
  "model_name": "llama3.1-8b",
  "placement": {
    "multibox": "dh1",
    "namespace": "inf-integ",
    "app_tag": "0.0.0+build.1b9c30c813"
  },
  "full_config": {
    "runconfig": {
      "transfer_processes": 8,
      "job_priority": "p1",
      "ini": {
        "inf_swdriver_placement": "infx"
      }
    },
    "api_config": {
      "workers": 4
    },
    "replica": {
      "concurrent_prompts": 4,
      "tbm_timeout_cumulative_ns": 10000000000,
      "health_polls": [
        "short",
        "long",
        "translate_and_feynman"
      ]
    },
    "model": {
      "name": "llama3.1-8b",
      "hf_model_dir": "/opt/cerebras/inference/models/Meta-Llama-3.1-8B-Instruct",
      "cerebras_checkpoint": "s3://inference-opensource/Meta-Llama-3.1-8B-Instruct/f16",
      "G": 1,
      "R": {
        "decoders": 8
      },
      "D": {
        "decoders": 4
      },
      "lanes": {
        "decoders": 2
      },
      "max_spread_x": 4,
      "spread_n": {
        "decoders": 14
      },
      "float_type": "f16",
      "max_context": 32768,
      "num_csx": 1,
      "dedicated_waf_deembed_core": true,
      "max_spread_y": 292,
      "structured_output": {
        "constrained_decoding_library": "llguidance",
        "lark_grammar_path": "models/llama3/llama3.1.lark.j2"
      },
      "completion_modes": [
        "Llama8BChatCompletionsMode"
      ]
    },
    "tokenizer": {
      "tokenizer_path": "/opt/cerebras/inference/models/Meta-Llama-3.1-8B-Instruct",
      "chat_template_relpath": "models/llama3/llama_3.1_8b.jinja"
    }
  },
  "wait_for_ready": true,
  "run_diagnostics": false
}'

# Response
{
  "replica_id": "abc123def456",
  "status": "pending",
  "message": "Replica abc123def456 created successfully"
}


# Check status (wait for compilation)
curl http://localhost:8000/api/v1/replicas/abc123def456 | jq

# Response
{
  "replica_id": "abc123def456",
  "status": "ready",
  "endpoint": "http://<user-node>:54137",
  "compile_wsjob": ["wsjob-compile-id"],
  "execute_wsjob": ["wsjob-execute-id"],
  ...
  "endpoint": "http://net004-us-sr04.sck2.cerebrascloud.com:54137",
```

## API Reference

### Base URL
```
http://localhost:8000/api/v1
```

### Endpoints

#### Create Replica
```http
POST /replicas
Content-Type: application/json

{
  "server_mode": "inference",           # Required: inference, api_gateway, platform_workload
  "model_name": "llama3.1-8b",         # Required: Model name
  "placement": {                        # Required: Where to run
    "app_tag": "0.0.0-build-abc123",   # Optional: App tag (for cbclient)
    "multibox": "dh1",                  # Optional: Multibox name
    "namespace": "inf-integ"            # Optional: K8s namespace
  },
  "cbclient_config": {                  # Optional: CBClient configuration
    "app_tag": "0.0.0-build-abc123",
    "use_uv": true
  },
  "full_config": {},                    # Optional: Model configuration
  "wait_for_ready": true,               # Optional: Wait for health check (default: false)
  "run_diagnostics": false              # Optional: Run diagnostics (default: false)
}

Response: 201 Created
{
  "replica_id": "abc123def456",
  "status": "pending",
  "message": "Replica abc123def456 created successfully",
  "base_url": null
}
```

#### List Replicas
```http
GET /replicas?server_mode=inference&status=ready&model_name=llama3.1-8b

Response: 200 OK
{
  "total": 2,
  "replicas": [
    {
      "replica_id": "abc123",
      "server_mode": "inference",
      "model_name": "llama3.1-8b",
      "status": "ready",
      "endpoint": "http://hostname:54137",
      "compile_wsjob": ["job1"],
      "execute_wsjob": ["job2"],
      ...
    }
  ]
}
```

#### Get Replica Status
```http
GET /replicas/{replica_id}

Response: 200 OK
{
  "replica_id": "abc123",
  "status": "ready",
  "info": {
    "replica_id": "abc123",
    "server_mode": "inference",
    "model_name": "llama3.1-8b",
    "status": "ready",
    "display_status": "Active",
    "endpoint": "http://net004-us-sr04.sck2.cerebrascloud.com:54137",
    "base_url": "http://127.0.0.1:54137",
    "host": null,
    "port": 54137,
    "workdir": "/n0/lab/test/dh1/abc123",
    "venv_path": "/n0/lab/test/dh1/abc123/cbclient",
    "compile_wsjob": ["wsjob-abc"],
    "execute_wsjob": ["wsjob-xyz"],
    "created_at": "2026-02-23T10:30:00Z",
    "ready_at": "2026-02-23T10:35:00Z"
  }
}
```

#### Stop Replica
```http
DELETE /replicas/{replica_id}

Response: 200 OK
{
  "replica_id": "abc123",
  "status": "stopped",
  "message": "Replica abc123 stopped successfully"
}
```

#### Service Health
```http
GET /health

Response: 200 OK
{
  "status": "healthy",
  "version": "0.1.0"
}
```

### Status Values

| Status | Description |
|--------|-------------|
| `pending` | Worker process starting |
| `creating` | Deploying environment |
| `starting` | Starting inference server |
| `waiting_for_ready` | Waiting for health check |
| `ready` | Replica is healthy and ready |
| `unhealthy` | Health check failed |
| `failed` | Replica creation failed |
| `stopped` | Replica explicitly stopped |

## Configuration

### Environment Variables

```bash
# Service configuration
HOST=0.0.0.0                    # Bind host (default: 0.0.0.0)
PORT=8000                       # Bind port (default: 8000)
LOG_LEVEL=INFO                  # Log level (default: INFO)

# Workdir configuration
LOCAL_WORKDIR_ROOT=/n0/lab/test/dh1  # Where to store replica workdirs

# Detached mode configuration
CLEANUP_ON_SHUTDOWN=false       # Kill replicas on shutdown (default: false)
```

### Config File

Create `config.yaml`:
```yaml
host: 0.0.0.0
port: 8000
log_level: INFO
local_workdir_root: /n0/lab/test/dh1
cleanup_on_shutdown: false      # true = kill replicas on shutdown
```

Load with:
```bash
export CONFIG_FILE=config.yaml
uvicorn release_server_service.main:app
```

## Detached Mode

### What is Detached Mode?

**Detached mode** means replicas and their wsjobs continue running even when the REST server is stopped or restarted.

### Default Behavior

**By default**, replicas run in **detached mode**:
- ✅ REST server shutdown does NOT kill replicas
- ✅ Replica processes continue running
- ✅ Wsjobs continue running on the cluster
- ✅ Long compilations (hours) are not interrupted
- ✅ Service can be upgraded without downtime

### Shutdown Behavior

```
REST Server Shutdown (SIGTERM/SIGINT)
    ↓
Stop monitoring task
    ↓
Exit REST server
    ↓
Workers continue running ✅
Inference servers continue running ✅
Wsjobs continue running ✅
```

### Restart Scenario

```
1. REST server running
2. Create replica → starts compilation (5 hours)
3. After 2 hours, upgrade REST server
4. Stop REST server
5. Upgrade code
6. Start REST server
7. Compilation continues (now at 3 hours) ✅
8. REST server reconnects to running replica ✅
9. Compilation completes normally ✅
```

### Cleanup Mode (Optional)

To kill replicas on shutdown, set:
```bash
export CLEANUP_ON_SHUTDOWN=true
```

Then on shutdown:
```
REST Server Shutdown
    ↓
Kill all worker processes
    ↓
Kill all inference servers
    ↓
Cancel all wsjobs ❌
```

**Use cleanup mode only for**:
- Development/testing
- Explicitly cleaning up after tests
- Controlled shutdowns where you want everything stopped

### Manual Cleanup

To stop a specific replica:
```bash
DELETE /api/v1/replicas/{replica_id}
```

To stop all replicas:
```bash
# List all replicas
curl http://localhost:8000/api/v1/replicas | jq -r '.replicas[].replica_id' > replica_ids.txt

# Stop each one
while read replica_id; do
  curl -X DELETE http://localhost:8000/api/v1/replicas/$replica_id
done < replica_ids.txt
```

## State Management

### State Storage

Replica state is stored in JSON files:
```
{workdir_root}/
  state/
    {replica_id}.json      # Replica state
    {replica_id}.pid       # Worker PID
  {replica_id}/            # Replica workdir
    request.json           # Original request
    cbclient/              # Python venv
    model_dir/
      cerebras_logs/
        run_meta.json      # Wsjob IDs
    worker_stdout.log      # Worker logs
    worker_stderr.log
    replica_stdout.log     # Replica logs
    replica_stderr.log
```

### State File Format

`state/{replica_id}.json`:
```json
{
  "server_mode": "inference",
  "model_name": "llama3.1-8b",
  "status": "ready",
  "display_status": "Active",
  "endpoint": "http://hostname:54137",
  "base_url": "http://127.0.0.1:54137",
  "port": 54137,
  "workdir": "/n0/lab/test/dh1/abc123",
  "venv_path": "/n0/lab/test/dh1/abc123/cbclient",
  "replica_pid": 12345,
  "compile_wsjob": ["wsjob-compile-id"],
  "execute_wsjob": ["wsjob-execute-id"],
  "created_at": "2026-02-23T10:30:00.000Z",
  "updated_at": "2026-02-23T10:35:00.000Z",
  "ready_at": "2026-02-23T10:35:00.000Z"
}
```

### State Lifecycle

```
1. Create → pending (state file created)
2. Worker starts → creating (state updated)
3. Server starts → starting (state updated)
4. Health check → waiting_for_ready (state updated)
5. Compilation starts → compile_wsjob added (state updated)
6. Execution starts → execute_wsjob added (state updated)
7. Ready → ready (state updated)
```

### State Persistence Benefits

- **Crash recovery**: Service can restart and find running replicas
- **No data loss**: All replica information preserved
- **Debugging**: Can inspect state files directly
- **Auditing**: Full history of replica lifecycle

## Troubleshooting

### Replica Logs

Check worker logs:
```bash
tail -f {workdir_root}/{replica_id}/worker_stdout.log
tail -f {workdir_root}/{replica_id}/worker_stderr.log
```

Check replica logs:
```bash
tail -f {workdir_root}/{replica_id}/replica_stdout.log
tail -f {workdir_root}/{replica_id}/replica_stderr.log
```

### Common Issues

#### 1. Package Not Found (devpi)
**Symptom**: `No solution found when resolving dependencies`
**Cause**: uv cannot resolve packages from devpi
**Solution**: Service now uses traditional pip (fixed)

#### 2. Invalid Docker Tag
**Symptom**: `couldn't parse image name ... invalid reference format`
**Cause**: Docker tags cannot contain `+` character
**Solution**: Service converts PEP 440 to Docker format (fixed)

#### 3. Execute Jobs Not Captured
**Symptom**: `execute_wsjob` always empty
**Cause**: Monitor stopped checking after finding compile jobs
**Solution**: Monitor now keeps checking until both types found (fixed)

#### 4. Endpoint Not Accessible
**Symptom**: `Connection refused` when connecting from remote machine
**Cause**: Endpoint using 127.0.0.1 instead of FQDN
**Solution**: Service converts to FQDN automatically (fixed)

### Debug Mode

Enable debug logging:
```bash
export LOG_LEVEL=DEBUG
uvicorn release_server_service.main:app
```

Check service logs:
```bash
tail -f /path/to/service.log | grep DEBUG
```

### Check Wsjob Status

From usernode:
```bash
# Check if wsjob is running
kubectl get pods -n {namespace} | grep wsjob

# Check wsjob logs
kubectl logs -n {namespace} wsjob-{id}-coordinator-0

# Check run_meta.json
cat {workdir}/{replica_id}/model_dir/cerebras_logs/run_meta.json | jq
```

## Development

### Project Structure

```
release-server-service/
├── src/release_server_service/
│   ├── main.py                      # FastAPI application
│   ├── config.py                    # Configuration
│   ├── api/
│   │   └── routes.py                # API endpoints
│   ├── core/
│   │   ├── replica_manager_v2.py    # Orchestrator
│   │   ├── replica_worker.py        # Worker process
│   │   ├── server_factory.py        # Server creation
│   │   ├── state_manager.py         # State persistence
│   │   ├── cbclient_deployer.py     # Environment setup
│   │   ├── wheel_resolver.py        # Package resolution
│   │   └── health.py                # Health checks
│   └── models/
│       ├── requests.py              # Request models
│       └── responses.py             # Response models
├── tests/                           # Tests
├── README.md                        # This file
├── requirements.txt                 # Dependencies
└── pyproject.toml                   # Package config
```

### Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/

# Run with coverage
pytest --cov=release_server_service tests/
```

### Contributing

1. Follow existing code style
2. Add tests for new features
3. Update documentation
4. Ensure all tests pass

### Debugging

Attach to worker process:
```bash
# Find worker PID
ps aux | grep replica_worker.py

# Attach with debugger
python -m pdb -p {PID}
```

Check state files:
```bash
# List all replicas
ls {workdir_root}/state/*.json

# View replica state
cat {workdir_root}/state/{replica_id}.json | jq
```

Monitor background task:
```bash
# Check monitoring logs
tail -f /path/to/service.log | grep "\[Monitor\]"
```

## Additional Documentation

See the following files for detailed information:

- `WORKER_PROCESS_ARCHITECTURE.md` - Worker process design
- `DETACHED_MODE_FIX.md` - True detached mode implementation
- `LOGGING_AND_WSJOB_FIX.md` - Wsjob tracking implementation
- `UV_DEVPI_RESOLUTION_FIX.md` - Package resolution fixes
- `DOCKER_TAG_FIX.md` - Docker image tag conversion
- `EXTERNAL_ENDPOINT_FIX.md` - FQDN endpoint resolution
- `EXECUTE_WSJOB_FIX.md` - Execute job tracking fix

## License

Cerebras Systems Inc. - Internal Use Only

## Support

For issues and questions:
- File an issue in the repository
- Contact the release engineering team
- Check the troubleshooting section above
