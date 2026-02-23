"""CBClient environment deployer.

Creates a Python virtual environment with the correct cbclient wheels
and dependencies installed — mirroring the monolith's deploy_client_env()
but for local execution (no SSH needed since the service is on the usernode).
"""

import asyncio
import logging
import os
import re
import sys
from typing import List, Optional

logger = logging.getLogger(__name__)

VENV_NAME = "cbclient"


class CBClientDeployResult:
    """Result of cbclient deployment."""

    def __init__(self, venv_path: str, python_exec: str):
        self.venv_path = venv_path
        self.python_exec = python_exec


def _resolve_version_from_tag(tag: str) -> Optional[str]:
    """Extract and convert app tag to valid PEP 440 version.

    Converts app_tag format to PEP 440 compliant version with local identifier.
    Format: X.Y.Z-suffix-suffix-... becomes X.Y.Z+suffix.suffix.suffix...

    Examples:
        "0.9.0+abc123" -> "0.9.0+abc123"
        "v0.9.0"       -> "0.9.0"
        "260113.3-inference-202602220136-2384-8fb6d540" -> "260113.3+inference.202602220136.2384.8fb6d540"
        "some-tag"     -> None
    """
    if not tag:
        return None
    # Strip leading 'v' if present
    cleaned = tag.lstrip("v")
    # Check for PEP 440-ish version: digits.digits...
    if not re.match(r"^\d+\.\d+", cleaned):
        return None

    # Convert app_tag format to PEP 440 local version
    # Replace first dash with + and remaining dashes with .
    if "-" in cleaned:
        parts = cleaned.split("-", 1)
        base_version = parts[0]
        local_part = parts[1].replace("-", ".")
        return f"{base_version}+{local_part}"

    return cleaned


def _resolve_version_from_whl(whl_path: str) -> Optional[str]:
    """Extract the version from a wheel filename.

    PEP 427 format: {name}-{version}(-{build})?-{python}-{abi}-{platform}.whl
    Example: cerebras_pytorch-2.3.0+12345-cp310-cp310-linux_x86_64.whl -> 2.3.0+12345
    """
    basename = os.path.basename(whl_path)
    parts = basename.split("-")
    if len(parts) >= 2:
        return parts[1]
    return None


async def _create_venv(venv_path: str, use_uv: bool = True, python_version: str = "3.11") -> None:
    """Create a virtual environment.

    Tries ``uv venv`` first (faster), falls back to ``python -m venv``.

    Args:
        venv_path: Path where the venv should be created
        use_uv: Whether to use uv (faster) or fall back to python -m venv
        python_version: Python version to use (default: 3.11 for cerebras_appliance wheels)
    """
    if use_uv:
        # Use uv with --python flag to specify Python version
        # uv will automatically download the correct Python version if not available
        proc = await asyncio.create_subprocess_exec(
            "uv", "venv", "--python", python_version, venv_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info(f"Created venv via uv with Python {python_version} at {venv_path}")
            return
        logger.warning(
            f"uv venv failed (rc={proc.returncode}), "
            f"falling back to python -m venv: {stderr.decode(errors='replace').strip()}"
        )

    # Fallback
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "venv", venv_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"venv creation failed (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    logger.info(f"Created venv via python -m venv at {venv_path}")


async def _ensure_pip(venv_path: str) -> None:
    """Ensure pip is installed in the venv.

    When venv is created with 'uv venv', pip is not installed by default.
    This function installs pip if it's not already present.
    """
    pip_exec = os.path.join(venv_path, "bin", "pip")
    if os.path.exists(pip_exec):
        logger.debug(f"pip already exists in venv: {pip_exec}")
        return

    logger.info(f"Installing pip in venv: {venv_path}")
    python_exec = os.path.join(venv_path, "bin", "python")

    proc = await asyncio.create_subprocess_exec(
        python_exec, "-m", "ensurepip", "--default-pip",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to install pip (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    logger.info(f"pip installed successfully in venv")


async def _pip_install(
    venv_path: str,
    packages: List[str],
    use_uv: bool = True,
    extra_args: Optional[List[str]] = None,
) -> None:
    """Install packages into the venv using pip or uv pip."""
    if not packages:
        return

    pip_exec = os.path.join(venv_path, "bin", "pip")
    extra = extra_args or []

    if use_uv:
        # Use --native-tls for compatibility with devpi and internal package indexes
        cmd = ["uv", "pip", "install", "--native-tls", "--python", os.path.join(venv_path, "bin", "python")] + extra + packages
    else:
        # Ensure pip is installed before using it
        await _ensure_pip(venv_path)
        cmd = [pip_exec, "install"] + extra + packages

    logger.info(f"Installing packages: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install failed (rc={proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()}"
        )
    logger.info(f"Installed {len(packages)} package(s)")


async def _install_requirements(
    venv_path: str,
    req_file: str,
    use_uv: bool = True,
) -> None:
    """Install packages from a requirements file."""
    if not os.path.exists(req_file):
        logger.warning(f"Requirements file not found: {req_file}")
        return
    await _pip_install(venv_path, ["-r", req_file], use_uv=use_uv)


async def deploy_cbclient_env(
    *,
    workdir: str,
    app_tag: Optional[str] = None,
    cbclient_whl: Optional[str] = None,
    client_version: Optional[str] = None,
    modelzoo_branch: Optional[str] = None,
    custom_requirements: Optional[List[str]] = None,
    use_uv: bool = True,
) -> CBClientDeployResult:
    """
    Deploy cbclient Python environment locally.

    Mirrors the monolith's ``_CBClient.deploy()`` but without SSH — we
    are already on the usernode.

    At least one of *app_tag*, *cbclient_whl*, or *client_version* must be
    provided so we know what to install.

    Steps:
        1. Resolve client version from the provided source(s).
        2. Create a venv (``uv venv`` or ``python -m venv``).
        3. Install cbclient wheel(s) into the venv.
        4. Install additional requirements (custom or modelzoo).
        5. Return :class:`CBClientDeployResult` with the venv python path.
    """
    # ── Step 0: Validate inputs ────────────────────────────────
    if not any([app_tag, cbclient_whl, client_version]):
        raise ValueError(
            "At least one of app_tag, cbclient_whl, or client_version "
            "must be provided to deploy_cbclient_env()"
        )

    # ── Step 1: Resolve version ────────────────────────────────
    resolved_version = client_version
    if not resolved_version and cbclient_whl:
        resolved_version = _resolve_version_from_whl(cbclient_whl)
    if not resolved_version and app_tag:
        resolved_version = _resolve_version_from_tag(app_tag)

    logger.info(
        f"[deploy] Deploying cbclient env: "
        f"version={resolved_version}, app_tag={app_tag}, "
        f"whl={cbclient_whl}, workdir={workdir}"
    )

    # ── Step 2: Create venv ────────────────────────────────────
    venv_path = os.path.join(workdir, VENV_NAME)
    await _create_venv(venv_path, use_uv=use_uv)

    # ── Step 3: Install cbclient wheel(s) ──────────────────────
    # NOTE: ALWAYS use traditional pip for cerebras-pytorch installation to avoid
    # uv's package resolution issues with internal devpi server. Even though venv
    # was created with 'uv venv', we install pip and use it directly.
    if cbclient_whl:
        logger.info(f"[deploy] Installing cbclient wheel: {cbclient_whl}")
        await _pip_install(venv_path, [cbclient_whl], use_uv=False)
    elif resolved_version:
        logger.info(f"[deploy] Installing cerebras-pytorch=={resolved_version} from devpi")
        await _pip_install(
            venv_path,
            [f"cerebras-pytorch=={resolved_version}"],
            use_uv=False,  # Force traditional pip for devpi packages
            extra_args=["--index-url", "https://devpi.cerebras.aws/root/main/+simple/"],
        )

    # ── Step 4: Install requirements ───────────────────────────
    if custom_requirements:
        logger.info(f"[deploy] Installing {len(custom_requirements)} custom requirements")
        # Write custom requirements to a temp file
        req_file = os.path.join(workdir, "requirements.txt")
        with open(req_file, "w") as f:
            for req in custom_requirements:
                f.write(req + "\n")
        await _install_requirements(venv_path, req_file, use_uv=use_uv)
    elif modelzoo_branch:
        # Look for a pre-existing requirements.txt in the workdir
        req_file = os.path.join(workdir, "requirements.txt")
        if os.path.exists(req_file):
            logger.info(f"[deploy] Installing requirements from {req_file}")
            await _install_requirements(venv_path, req_file, use_uv=use_uv)
        else:
            logger.info(
                f"[deploy] No requirements.txt found for modelzoo_branch={modelzoo_branch} "
                "(skipping additional dependencies)"
            )

    # ── Step 5: Build result ───────────────────────────────────
    python_exec = os.path.join(venv_path, "bin", "python")
    logger.info(f"[deploy] ✓ CBClient env deployed: python_exec={python_exec}")

    return CBClientDeployResult(
        venv_path=venv_path,
        python_exec=python_exec,
    )
