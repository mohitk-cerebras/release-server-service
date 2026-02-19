"""
Health check utilities for server handles."""

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


async def poll_health_endpoint(
    base_url: str,
    timeout_s: int = 120,
    poll_interval_s: int = 5,
) -> bool:
    """
    Poll a server's health endpoint until it responds successfully.

    This is a standalone implementation that doesn't depend on monolith code.
    It mirrors the behavior of InferenceServerHandle.health_check().

    Args:
        base_url: The base URL of the server (e.g., http://host:port)
        timeout_s: Maximum time to wait for a healthy response
        poll_interval_s: Time between poll attempts

    Returns:
        True if the server is healthy, False if timeout was reached
    """
    health_url = f"{base_url}/health"
    deadline = asyncio.get_event_loop().time() + timeout_s
    attempt = 0

    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    health_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=False,
                ) as resp:
                    if resp.status == 200:
                        logger.info(
                            f"Health check passed on attempt {attempt}: {health_url}"
                        )
                        return True
                    else:
                        logger.debug(
                            f"Health check attempt {attempt}: status={resp.status}"
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.debug(f"Health check attempt {attempt} failed: {e}")

        await asyncio.sleep(poll_interval_s)

    logger.error(f"Health check timed out after {timeout_s}s ({attempt} attempts)")
    return False


async def run_diagnostics(base_url: str) -> Optional[dict]:
    """
    Run diagnostics against a server endpoint.

    Calls the /diagnostics endpoint if available. This mirrors
    InferenceServerHandle.run_diagnostics().

    Args:
        base_url: Server base URL

    Returns:
        Diagnostics dict or None if not available
    """
    diag_url = f"{base_url}/diagnostics"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                diag_url,
                timeout=aiohttp.ClientTimeout(total=30),
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    logger.debug(f"Diagnostics endpoint returned {resp.status}")
                    return None
    except Exception as e:
        logger.debug(f"Diagnostics not available: {e}")
        return None
