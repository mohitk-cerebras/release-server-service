"""Resolver — mirrors monolith's cb_client_utils.py for app_tag → wheel resolution.

Resolution chain:
  app_tag → whl_version → whl_filename → local_path

References:
  - get_whl_version_from_app_tag()  (monolith cb_client_utils.py#L635-L703)
  - get_whl_name_from_version()     (monolith cb_client_utils.py#L590-L605)
  - find_local_cbwhl()              (monolith cb_client_utils.py#L145-L177)
"""

import glob
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Default Python version tag for wheel filenames
_PY_VER_TAG = f"cp{sys.version_info.major}{sys.version_info.minor}"

# Default platform tag
_PLATFORM_TAG = "linux_x86_64"

# Artifact cache base path (mirrors monolith CB_ARTIFACTS_BUILDS_CBCORE_DIR)
_ARTIFACT_CACHE_BASE = Path("/cb/artifacts/builds/cbcore")

# Release-stage artifacts (mirrors monolith COLO_ARTIFACTS_DR)
_COLO_ARTIFACTS_DIR = Path("/cb/artifacts/release-stage")


def _get_git_top() -> Optional[str]:
    """Get the git repository root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return None


def _get_workspace_appliance_tag() -> Optional[str]:
    """Get the workspace appliance tag: ``<user>-<git-short-hash>``.

    Mirrors monolith's ``get_cluster_mgmt_appliance_tag_from_workspace()``.
    Returns None if git info is unavailable.
    """
    try:
        git_top = _get_git_top()
        if not git_top:
            return None
        user = os.environ.get("USER") or os.getlogin()
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=git_top,
        )
        if result.returncode == 0:
            githash = result.stdout.strip()
            return f"{user}-{githash}"
    except Exception:
        pass
    return None


def _get_whl_version_from_workspace() -> Optional[str]:
    """Read wheel version from ``build/appliance/appliance-tag`` file.

    Mirrors monolith's ``get_whl_version_from_workspace()``.
    Returns the content of the tag file, or None if it doesn't exist.
    """
    git_top = _get_git_top()
    if not git_top:
        return None
    tag_file = Path(git_top) / "build" / "appliance" / "appliance-tag"
    if tag_file.exists():
        version = tag_file.read_text().strip()
        if version:
            logger.info(
                f"[wheel_resolver] Read version from {tag_file}: '{version}'"
            )
            return version
    return None


def _try_ecr_metadata_lookup(app_tag: str) -> Optional[str]:
    """
    Try to resolve version from ECR docker image metadata.

    Mirrors monolith's ``_try_get_client_version_from_docker()``.
    Looks for the 'net.cerebras.client_version' label on the
    cbcore:{app_tag} image.

    This is best-effort — if docker/skopeo isn't available or the
    image doesn't exist, returns None.
    """
    try:
        # Try skopeo first (doesn't require docker daemon)
        result = subprocess.run(
            [
                "skopeo", "inspect",
                f"docker://cbcore:{app_tag}",
                "--format", '{{index .Labels "net.cerebras.client_version"}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            version = result.stdout.strip()
            logger.info(
                f"[wheel_resolver] Resolved '{app_tag}' → '{version}' "
                f"(ECR metadata via skopeo)"
            )
            return version
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    try:
        # Fallback to docker inspect
        result = subprocess.run(
            [
                "docker", "inspect",
                f"cbcore:{app_tag}",
                "--format",
                '{{index .Config.Labels "net.cerebras.client_version"}}',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            version = result.stdout.strip()
            logger.info(
                f"[wheel_resolver] Resolved '{app_tag}' → '{version}' "
                f"(ECR metadata via docker)"
            )
            return version
    except (subprocess.SubprocessError, FileNotFoundError):
        pass

    return None


def get_whl_version_from_app_tag(app_tag: str) -> Optional[str]:
    """
    Look-up or guess the client version.

    Mirrors monolith's ``get_whl_version_from_app_tag()`` logic exactly:

    1. If tag contains '+', it's already a whl version → return as-is
    2. If tag matches current workspace tag → read version from
       ``build/appliance/appliance-tag`` file
    3. Try ECR docker metadata lookup
    4. If no '-' in tag → use as-is (public version like '2.0.1')
    5. Fallback heuristic: split on ALL '-', last element is githash,
       first element is ver. ``ver, *other, githash = tag.split("-")``
       - If ``other`` is empty (tag was ``prefix-hash``), set ver='0.0.0'
       - Return ``{ver}+{githash}``

    Args:
        app_tag: The application tag (e.g., 'build-1b83940b24',
                 '0.0.0-202304182329-3859-2c8823fc', '2.3.0')

    Returns:
        PEP 440 version string or None
    """
    if not app_tag:
        return None

    # 1. Already a local version (contains '+')
    if "+" in app_tag:
        logger.debug(
            f"[wheel_resolver] Tag '{app_tag}' already contains '+', "
            f"using as-is"
        )
        return app_tag

    # 2. Check if this is the current workspace tag
    workspace_tag = _get_workspace_appliance_tag()
    if workspace_tag and app_tag == workspace_tag:
        workspace_version = _get_whl_version_from_workspace()
        if workspace_version:
            logger.info(
                f"[wheel_resolver] Tag '{app_tag}' matches workspace tag, "
                f"resolved to '{workspace_version}' from appliance-tag file"
            )
            return workspace_version
        # Workspace tag matched but no appliance-tag file — fall through
        # to docker/heuristic
        logger.warning(
            f"[wheel_resolver] Tag '{app_tag}' matches workspace tag but "
            f"appliance-tag file not found, trying docker metadata..."
        )
        client_version = _try_ecr_metadata_lookup(app_tag)
        if client_version:
            return client_version

    # 3. Try to read from docker image metadata in ECR
    client_version = _try_ecr_metadata_lookup(app_tag)
    if client_version:
        return client_version
    else:
        logger.debug(
            f"[wheel_resolver] Couldn't read docker image metadata for "
            f"'{app_tag}'"
        )

    # 4. If no '-' in tag, check if it's a valid PEP 440 version
    if "-" not in app_tag:
        # Try to validate as PEP 440 version if packaging is available
        try:
            import packaging.version
            packaging.version.parse(app_tag)
            # Valid PEP 440 version, use as-is
            logger.debug(
                f"[wheel_resolver] Tag '{app_tag}' is a valid PEP 440 version"
            )
            return app_tag
        except ImportError:
            # packaging not available, use simple heuristic
            # If it starts with a digit and contains dots, likely a version
            if app_tag and app_tag[0].isdigit() and '.' in app_tag:
                logger.debug(
                    f"[wheel_resolver] Tag '{app_tag}' looks like a version "
                    f"(starts with digit, contains dots)"
                )
                return app_tag
            # Otherwise, it's not recognizable
            logger.warning(
                f"[wheel_resolver] Tag '{app_tag}' is not a recognized format "
                f"and packaging module not available"
            )
            return None
        except Exception:
            # packaging available but parse failed - not a valid version
            logger.warning(
                f"[wheel_resolver] Tag '{app_tag}' is not a valid version "
                f"and no docker metadata found"
            )
            return None

    # 5. Fallback: guess based on legacy whl version scheme
    #    Format: {version}-{component}-{timestamp}-{build_num}-{githash}
    #    Example: 260110.dev1-inference-202602192107-2371-c14727f0
    #    Result: 260110.dev1+inference.202602192107.2371.c14727f0
    logger.warning(
        f"[wheel_resolver] Falling back to guessing the python version "
        f"for '{app_tag}'"
    )
    parts = app_tag.split("-")

    # Check if first part looks like a version number
    # A version number typically starts with a digit and may contain dots
    first_part = parts[0]
    if first_part and first_part[0].isdigit():
        # First part starts with a digit, likely a version number
        base_version = first_part
    else:
        # First part is not a version (e.g., 'build', 'release', 'nightly')
        base_version = "0.0.0"

    # Join all remaining parts (component, timestamp, build_num, githash) with dots
    # This creates the local version identifier
    if len(parts) > 1:
        local_parts = ".".join(parts[1:])
        return f"{base_version}+{local_parts}"
    else:
        # Single part, no local version
        return base_version


def get_whl_name_from_version(
    version: str,
    py_ver_tag: Optional[str] = None,
    platform_tag: Optional[str] = None,
) -> str:
    """
    Compose a wheel filename from a version string.

    Mirrors monolith's ``get_whl_name_from_version()``:
      cerebras_appliance-{version}-cp311-cp311-linux_x86_64.whl

    Uses ``packaging.version.parse()`` to normalize the version
    (matching the normalization that happens when writing .whl files),
    then composes the filename.

    Args:
        version: PEP 440 version string (e.g., '2.3.0+abc123')
        py_ver_tag: Python version tag (e.g., 'cp311'), defaults to current
        platform_tag: Platform tag, defaults to 'linux_x86_64'

    Returns:
        Wheel filename string
    """
    py_tag = py_ver_tag or _PY_VER_TAG
    plat_tag = platform_tag or _PLATFORM_TAG

    # Normalize version via PEP 440 (mirrors monolith)
    try:
        import packaging.version

        normalized = str(packaging.version.parse(version))
        if normalized != version:
            logger.warning(
                f"[wheel_resolver] Using normalized version "
                f"'{normalized}' instead of '{version}'"
            )
            version = normalized
    except Exception:
        # packaging not available or parse failed — use version as-is
        logger.debug(
            f"[wheel_resolver] Could not normalize version '{version}', "
            f"using as-is"
        )

    # PEP 427: Replace '+' with '.' in local version identifiers for wheel filename
    version_for_filename = version.replace("+", ".")

    # Note: Cerebras wheels can be either:
    # 1. Platform-specific: cerebras_appliance-{version}-cp311-cp311-linux_x86_64.whl
    # 2. Pure Python: cerebras_appliance-{version}-cp311-none-any.whl
    # This function returns the platform-specific format, but the resolver
    # will also try wildcards to find pure Python wheels
    filename = (
        f"cerebras_appliance-{version_for_filename}-"
        f"{py_tag}-{py_tag}-{plat_tag}.whl"
    )
    logger.debug(f"[wheel_resolver] Wheel filename (platform-specific): {filename}")
    return filename


def _collect_candidate_wheels(
    whl_glob: str,
    git_top: Optional[str] = None,
    artifact_cache_base: Optional[str] = None,
) -> List[Path]:
    """
    Collect candidate wheel files matching a glob pattern from all search locations.

    Searches:
    1. Workspace build directory
    2. Artifact cache (version-stamped directories)
    3. Release-stage artifacts (3-level structure: component/version/timestamp-build-hash)
    4. Flat layout under cache base
    """
    cache_base = Path(artifact_cache_base) if artifact_cache_base else _ARTIFACT_CACHE_BASE
    candidates: List[Path] = []

    top = git_top or _get_git_top()
    if top:
        candidates.extend(Path(top).glob(f"build/appliance/{whl_glob}"))

    candidates.extend(cache_base.glob(f"2*/{whl_glob}"))

    # Release-stage: 3-level structure (component/version/timestamp-build-hash/components/...)
    # Try both with and without cbcore subdirectory
    candidates.extend(_COLO_ARTIFACTS_DIR.glob(f"*/*/*/components/cbcore/{whl_glob}"))
    candidates.extend(_COLO_ARTIFACTS_DIR.glob(f"*/*/*/components/{whl_glob}"))

    flat_path = cache_base / whl_glob
    candidates.extend(flat_path.parent.glob(flat_path.name))

    return [path for path in candidates if path.is_file()]


def _pick_preferred_wheel(
    candidates: List[Path],
    preferred_py_tag: str,
) -> Optional[Path]:
    if not candidates:
        return None

    def sort_key(path: Path) -> tuple:
        name = path.name
        preferred = preferred_py_tag in name
        return (not preferred, name)

    return sorted(candidates, key=sort_key)[0]


def find_local_cbwhl(
    whl_filename: str,
    git_top: Optional[str] = None,
    artifact_cache_base: Optional[str] = None,
) -> Optional[str]:
    """
    Search for a local wheel file.

    Mirrors monolith's ``find_local_cbwhl()`` search order:
    1. ``{git_top}/build/appliance/{whl_filename}`` (workspace build)
    2. ``/cb/artifacts/builds/cbcore/2*/{whl_filename}`` (artifact cache)
    3. ``/cb/artifacts/release-stage/*/*/components/cbcore/{whl_filename}``
       (release-stage artifacts, mirrors monolith COLO_ARTIFACTS_DR)
    4. ``/cb/artifacts/builds/cbcore/{whl_filename}`` (flat layout)

    Args:
        whl_filename: The wheel filename to search for
        git_top: Git repository root (auto-detected if not provided)
        artifact_cache_base: Artifact cache base path

    Returns:
        Full path to the wheel file, or None if not found
    """
    cache_base = Path(artifact_cache_base) if artifact_cache_base else _ARTIFACT_CACHE_BASE

    logger.debug(f"[wheel_resolver] Searching for wheel: {whl_filename}")

    # 1. Workspace build directory
    top = git_top or _get_git_top()
    if top:
        workspace_path = Path(top) / "build" / "appliance" / whl_filename
        logger.debug(f"[wheel_resolver]   Checking workspace: {workspace_path}")
        if workspace_path.is_file():
            logger.info(
                f"[wheel_resolver] Found wheel in workspace: {workspace_path}"
            )
            return str(workspace_path)
    else:
        logger.debug(f"[wheel_resolver]   No git top found, skipping workspace")

    # 2. Artifact cache (glob for version-stamped dirs)
    #    Mirrors monolith: CB_ARTIFACTS_BUILDS_CBCORE_DIR.glob(f"2*/{whl_filename}")
    #    Excludes the 'latest' symlink by only matching dirs starting with '2'
    if cache_base.exists():
        search_pattern = cache_base / "2*" / whl_filename
        logger.debug(f"[wheel_resolver]   Checking artifact cache: {search_pattern}")
        maybe_whl_paths = sorted(
            cache_base.glob(f"2*/{whl_filename}"), reverse=True
        )
        if maybe_whl_paths:
            path = maybe_whl_paths[0]
            logger.info(f"[wheel_resolver] Found wheel in artifact cache: {path}")
            return str(path)
        else:
            logger.debug(f"[wheel_resolver]   No match in artifact cache (pattern: {search_pattern})")
    else:
        logger.debug(f"[wheel_resolver]   Skipping artifact cache (directory not accessible): {cache_base}")

    # 3. Release-stage artifacts
    #    Mirrors monolith COLO_ARTIFACTS_DR: /cb/artifacts/release-stage/
    #    Pattern matches: /{component}/{version}/{timestamp-build-hash}/components/...
    #    Example: /inference/260215/202602201519-2373-9999f993/components/

    if _COLO_ARTIFACTS_DIR.exists():
        # Try with cbcore subdirectory first
        release_pattern = _COLO_ARTIFACTS_DIR / "*" / "*" / "*" / "components" / "cbcore" / whl_filename
        logger.debug(f"[wheel_resolver]   Checking release-stage (cbcore): {release_pattern}")
        release_stage_paths = sorted(
            _COLO_ARTIFACTS_DIR.glob(f"*/*/*/components/cbcore/{whl_filename}"),
            reverse=True,
        )

        # If not found, try without cbcore subdirectory
        if not release_stage_paths:
            release_pattern2 = _COLO_ARTIFACTS_DIR / "*" / "*" / "*" / "components" / whl_filename
            logger.debug(f"[wheel_resolver]   Checking release-stage (direct): {release_pattern2}")
            release_stage_paths = sorted(
                _COLO_ARTIFACTS_DIR.glob(f"*/*/*/components/{whl_filename}"),
                reverse=True,
            )

        if release_stage_paths:
            path = release_stage_paths[0]  # Take the first (most recent) after reverse sort
            logger.info(
                f"[wheel_resolver] Found wheel in release-stage: {path}"
            )
            return str(path)
        else:
            logger.debug(f"[wheel_resolver]   No match in release-stage (tried both patterns)")
    else:
        logger.debug(f"[wheel_resolver]   Skipping release-stage (directory not accessible): {_COLO_ARTIFACTS_DIR}")

    # 4. Flat layout under cache base
    if cache_base.exists():
        flat_path = cache_base / whl_filename
        logger.debug(f"[wheel_resolver]   Checking flat layout: {flat_path}")
        if flat_path.is_file():
            logger.info(
                f"[wheel_resolver] Found wheel in cache (flat): {flat_path}"
            )
            return str(flat_path)

    logger.warning(
        f"[wheel_resolver] Wheel '{whl_filename}' not found in any location. "
        f"Searched: workspace, artifact cache ({cache_base}/2*/), "
        f"release-stage ({_COLO_ARTIFACTS_DIR}/**/), flat layout"
    )
    return None


def resolve_wheel_path(app_tag: str) -> Optional[str]:
    """
    Full resolution chain: app_tag → version → filename → local path.

    This is the main entry point for callers.

    Args:
        app_tag: Application tag (e.g., 'build-1b83940b24')

    Returns:
        Full path to the local wheel file, or None
    """
    version = get_whl_version_from_app_tag(app_tag)
    if not version:
        logger.error(
            f"[wheel_resolver] Cannot resolve version for tag '{app_tag}'"
        )
        return None

    whl_name = get_whl_name_from_version(version)
    whl_path = find_local_cbwhl(whl_name)

    if not whl_path:
        # Fallback: search for any wheel matching the resolved version
        # Note: Glob patterns may not handle '+' in filenames well, so we try multiple approaches

        # Extract base version (before the '+')
        base_version = version.split("+")[0] if "+" in version else version

        patterns_to_try = [
            # === Try with PEP 427 format (+ → .) ===
            # Pattern 1: Pure Python wheels (most common for inference builds)
            f"cerebras_appliance-{version.replace('+', '.')}-cp3*-none-any.whl",
            # Pattern 2: Platform-specific
            f"cerebras_appliance-{version.replace('+', '.')}-cp3*-cp3*-{_PLATFORM_TAG}.whl",
            # Pattern 3: Any wheel for this version
            f"cerebras_appliance-{version.replace('+', '.')}-*.whl",

            # === Try with base version only (works around glob + issues) ===
            # Pattern 4: Match just base version + wildcard
            f"cerebras_appliance-{base_version}*-cp3*-none-any.whl",
            # Pattern 5: Match base version + any tags
            f"cerebras_appliance-{base_version}*-*.whl",
        ]

        for pattern in patterns_to_try:
            logger.debug(
                f"[wheel_resolver] Exact wheel '{whl_name}' not found, "
                f"trying fallback pattern: {pattern}"
            )
            candidates = _collect_candidate_wheels(pattern)
            if candidates:
                logger.debug(
                    f"[wheel_resolver] Found {len(candidates)} candidate(s) "
                    f"matching pattern: {pattern}"
                )
                # Filter candidates to match the full version
                filtered = [
                    c for c in candidates
                    if version.replace("+", ".") in c.name or version.replace("+", "+") in c.name
                ]
                if filtered:
                    logger.debug(
                        f"[wheel_resolver] Filtered to {len(filtered)} candidate(s) "
                        f"matching version '{version}'"
                    )
                    preferred = _pick_preferred_wheel(filtered, _PY_VER_TAG)
                else:
                    preferred = _pick_preferred_wheel(candidates, _PY_VER_TAG)

                if preferred:
                    whl_path = str(preferred)
                    logger.info(
                        f"[wheel_resolver] Found compatible wheel for '{version}' "
                        f"using fallback search: {whl_path}"
                    )
                    break

    if whl_path:
        logger.info(
            f"[wheel_resolver] Resolved: '{app_tag}' → '{version}' "
            f"→ '{whl_path}'"
        )
    else:
        logger.error(
            f"[wheel_resolver] Resolved version '{version}' but wheel "
            f"'{whl_name}' not found locally"
        )

    return whl_path
