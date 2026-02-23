#!/usr/bin/env python3
# Requires: PyYAML (pip install pyyaml)
"""
Convert a model configuration YAML file to CreateReplicaRequest JSON format.

Usage:
    python convert_config.py <config.yaml> [options]

Examples:
    # Basic usage
    python convert_config.py model_config.yaml

    # With custom placement
    python convert_config.py model_config.yaml \
        --app-tag 260215-inference-202602201519-2373-9999f993 \
        --multibox oly \
        --namespace inf-integ

    # Output to file
    python convert_config.py model_config.yaml -o request.json
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:
    print("Error: PyYAML is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def load_yaml(file_path: str) -> Dict[str, Any]:
    """Load YAML configuration from file."""
    with open(file_path, 'r') as f:
        return yaml.safe_load(f)


def extract_model_name(config: Dict[str, Any]) -> str:
    """Extract model name from config, with fallback."""
    if 'model' in config and 'name' in config['model']:
        return config['model']['name']
    return "unknown-model"


def extract_job_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Extract job configuration from runconfig."""
    job_config = {
        "job_priority": "p2",
        "job_timeout_s": 86400,
        "job_labels": None
    }

    if 'runconfig' in config:
        runconfig = config['runconfig']
        if 'job_priority' in runconfig:
            job_config['job_priority'] = runconfig['job_priority']
        if 'job_time_sec' in runconfig:
            job_config['job_timeout_s'] = runconfig['job_time_sec']
        if 'job_labels' in runconfig:
            job_config['job_labels'] = runconfig['job_labels']

    return job_config


def convert_to_request(
    config: Dict[str, Any],
    server_mode: str = "replica",
    multibox: str = "oly",
    namespace: str = "inf-integ",
    app_tag: str = None,
    wait_for_ready: bool = True,
    run_diagnostics: bool = False,
) -> Dict[str, Any]:
    """Convert model config to CreateReplicaRequest format."""

    # Extract model name
    model_name = extract_model_name(config)

    # Extract job configuration
    job_config = extract_job_config(config)

    # Build the request
    request = {
        "server_mode": server_mode,
        "model_name": model_name,
        "placement": {
            "multibox": multibox,
            "namespace": namespace
        },
        "full_config": config,
        "job": job_config,
        "wait_for_ready": wait_for_ready,
        "run_diagnostics": run_diagnostics
    }

    # Add app_tag if provided
    if app_tag:
        request["placement"]["app_tag"] = app_tag
        request["cbclient_config"] = {
            "app_tag": app_tag,
            "use_uv": True
        }

    return request


def main():
    parser = argparse.ArgumentParser(
        description="Convert model config YAML to CreateReplicaRequest JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s model_config.yaml
  %(prog)s model_config.yaml --app-tag 260215-inference-202602201519-2373-9999f993
  %(prog)s model_config.yaml -o request.json --multibox oly --namespace inf-integ
        """
    )

    parser.add_argument(
        'config_file',
        help='Path to model configuration YAML file'
    )

    parser.add_argument(
        '-o', '--output',
        help='Output JSON file (default: stdout)',
        default=None
    )

    parser.add_argument(
        '--server-mode',
        help='Server mode (default: replica)',
        default='replica',
        choices=['replica', 'api_gateway', 'platform_workload', 'catalog']
    )

    parser.add_argument(
        '--multibox',
        help='Target multibox/cluster name (default: oly)',
        default='oly'
    )

    parser.add_argument(
        '--namespace',
        help='Kubernetes namespace (default: inf-integ)',
        default='inf-integ'
    )

    parser.add_argument(
        '--app-tag',
        help='Application tag for deployment (e.g., 260215-inference-202602201519-2373-9999f993)',
        default=None
    )

    parser.add_argument(
        '--wait-for-ready',
        help='Wait for server to be ready (default: true)',
        action='store_true',
        default=True
    )

    parser.add_argument(
        '--no-wait-for-ready',
        help='Do not wait for server to be ready',
        action='store_false',
        dest='wait_for_ready'
    )

    parser.add_argument(
        '--run-diagnostics',
        help='Run diagnostics after server is ready',
        action='store_true',
        default=False
    )

    parser.add_argument(
        '--indent',
        help='JSON indentation (default: 2)',
        type=int,
        default=2
    )

    args = parser.parse_args()

    # Check if config file exists
    config_path = Path(args.config_file)
    if not config_path.exists():
        print(f"Error: Config file not found: {args.config_file}", file=sys.stderr)
        sys.exit(1)

    # Load the YAML config
    try:
        config = load_yaml(args.config_file)
    except Exception as e:
        print(f"Error loading YAML: {e}", file=sys.stderr)
        sys.exit(1)

    # Convert to request format
    request = convert_to_request(
        config=config,
        server_mode=args.server_mode,
        multibox=args.multibox,
        namespace=args.namespace,
        app_tag=args.app_tag,
        wait_for_ready=args.wait_for_ready,
        run_diagnostics=args.run_diagnostics
    )

    # Output JSON
    json_output = json.dumps(request, indent=args.indent)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(json_output)
        print(f"✓ Wrote request to {args.output}", file=sys.stderr)
    else:
        print(json_output)


if __name__ == '__main__':
    main()
