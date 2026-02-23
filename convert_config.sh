#!/bin/bash
set -euo pipefail

# Convert model configuration YAML to CreateReplicaRequest JSON format
#
# Usage:
#   ./convert_config.sh <config.yaml> [options]
#
# Examples:
#   ./convert_config.sh model_config.yaml
#   ./convert_config.sh model_config.yaml --app-tag 260215-inference-202602201519-2373-9999f993
#   ./convert_config.sh model_config.yaml -o request.json --multibox oly

# Default values
SERVER_MODE="replica"
MULTIBOX="oly"
NAMESPACE="inf-integ"
APP_TAG=""
OUTPUT_FILE=""
WAIT_FOR_READY="true"
RUN_DIAGNOSTICS="false"

# Check for required commands
for cmd in yq jq; do
    if ! command -v $cmd &> /dev/null; then
        echo "Error: $cmd is required but not installed" >&2
        echo "Install with:" >&2
        echo "  yq: pip install yq  OR  brew install yq" >&2
        echo "  jq: sudo yum install jq  OR  brew install jq" >&2
        exit 1
    fi
done

# Help message
show_help() {
    cat << EOF
Usage: ${0##*/} <config.yaml> [options]

Convert model configuration YAML to CreateReplicaRequest JSON format.

Arguments:
    config.yaml              Path to model configuration YAML file

Options:
    -o, --output FILE       Output JSON file (default: stdout)
    --server-mode MODE      Server mode: replica, api_gateway, platform_workload, catalog (default: replica)
    --multibox NAME         Target multibox/cluster name (default: oly)
    --namespace NS          Kubernetes namespace (default: inf-integ)
    --app-tag TAG           Application tag for deployment
    --no-wait               Do not wait for server to be ready
    --run-diagnostics       Run diagnostics after server is ready
    -h, --help              Show this help message

Examples:
    ${0##*/} model_config.yaml
    ${0##*/} model_config.yaml --app-tag 260215-inference-202602201519-2373-9999f993
    ${0##*/} model_config.yaml -o request.json --multibox oly --namespace inf-integ
EOF
}

# Parse arguments
CONFIG_FILE=""
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        -o|--output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --server-mode)
            SERVER_MODE="$2"
            shift 2
            ;;
        --multibox)
            MULTIBOX="$2"
            shift 2
            ;;
        --namespace)
            NAMESPACE="$2"
            shift 2
            ;;
        --app-tag)
            APP_TAG="$2"
            shift 2
            ;;
        --no-wait)
            WAIT_FOR_READY="false"
            shift
            ;;
        --run-diagnostics)
            RUN_DIAGNOSTICS="true"
            shift
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            show_help >&2
            exit 1
            ;;
        *)
            if [[ -z "$CONFIG_FILE" ]]; then
                CONFIG_FILE="$1"
            else
                echo "Error: Multiple config files specified" >&2
                exit 1
            fi
            shift
            ;;
    esac
done

# Validate config file
if [[ -z "$CONFIG_FILE" ]]; then
    echo "Error: Config file not specified" >&2
    show_help >&2
    exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: Config file not found: $CONFIG_FILE" >&2
    exit 1
fi

# Read the YAML config and convert to JSON
FULL_CONFIG=$(yq -o json '.' "$CONFIG_FILE")

# Extract model name from config
MODEL_NAME=$(echo "$FULL_CONFIG" | jq -r '.model.name // "unknown-model"')

# Extract job configuration from runconfig
JOB_PRIORITY=$(echo "$FULL_CONFIG" | jq -r '.runconfig.job_priority // "p2"')
JOB_TIMEOUT=$(echo "$FULL_CONFIG" | jq -r '.runconfig.job_time_sec // 86400')
JOB_LABELS=$(echo "$FULL_CONFIG" | jq -c '.runconfig.job_labels // null')

# Build the base request
REQUEST=$(jq -n \
    --arg server_mode "$SERVER_MODE" \
    --arg model_name "$MODEL_NAME" \
    --arg multibox "$MULTIBOX" \
    --arg namespace "$NAMESPACE" \
    --argjson full_config "$FULL_CONFIG" \
    --arg job_priority "$JOB_PRIORITY" \
    --argjson job_timeout "$JOB_TIMEOUT" \
    --argjson job_labels "$JOB_LABELS" \
    --argjson wait_for_ready "$WAIT_FOR_READY" \
    --argjson run_diagnostics "$RUN_DIAGNOSTICS" \
    '{
        server_mode: $server_mode,
        model_name: $model_name,
        placement: {
            multibox: $multibox,
            namespace: $namespace
        },
        full_config: $full_config,
        job: {
            job_priority: $job_priority,
            job_timeout_s: $job_timeout,
            job_labels: $job_labels
        },
        wait_for_ready: $wait_for_ready,
        run_diagnostics: $run_diagnostics
    }'
)

# Add app_tag and cbclient_config if app_tag is provided
if [[ -n "$APP_TAG" ]]; then
    REQUEST=$(echo "$REQUEST" | jq \
        --arg app_tag "$APP_TAG" \
        '.placement.app_tag = $app_tag |
         .cbclient_config = {
             app_tag: $app_tag,
             use_uv: true
         }'
    )
fi

# Output the result
if [[ -n "$OUTPUT_FILE" ]]; then
    echo "$REQUEST" | jq '.' > "$OUTPUT_FILE"
    echo "✓ Wrote request to $OUTPUT_FILE" >&2
else
    echo "$REQUEST" | jq '.'
fi
