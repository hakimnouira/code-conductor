"""CloakBrowser and Chromium Stealth Launcher.

Handles discovering and launching CloakBrowser (or standard Chrome/Edge)
with remote debugging port 9222 and persistent user data directory,
so logins and cookies remain permanently saved across sessions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_CDP_PORT = 9222
DEFAULT_PROFILE_DIR = Path(r"D:\work\code-conductor\.cloak_browser_profile")

CLOAK_CANDIDATE_PATHS = [
    Path(r"D:\Downloads\cloakbrowser-windows-x64\chrome.exe"),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-1243\chrome-win64\chrome.exe")),
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe")),
    Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe")),
]


def find_browser_executable() -> Path | None:
    """Finds CloakBrowser or best available Chromium browser binary."""
    for path in CLOAK_CANDIDATE_PATHS:
        if path.exists() and path.is_file():
            return path
    which_chrome = shutil.which("chrome") or shutil.which("msedge") or shutil.which("google-chrome")
    if which_chrome:
        return Path(which_chrome)
    return None


async def is_cdp_connected(port: int = DEFAULT_CDP_PORT, timeout: float = 1.0) -> bool:
    """Checks if Chrome DevTools Protocol is actively responding on localhost."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
            return resp.status_code == 200
    except Exception:
        return False


async def launch_cloakbrowser(
    port: int = DEFAULT_CDP_PORT,
    profile_dir: Path | None = None,
    open_url: str = "about:blank",
) -> dict[str, Any]:
    """Launches CloakBrowser with persistent profile and remote debugging enabled."""
    if await is_cdp_connected(port):
        if open_url and open_url != "about:blank":
            try:
                import urllib.parse
                async with httpx.AsyncClient(timeout=3.0) as client:
                    await client.put(f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(open_url, safe=':/?=&%')}")
            except Exception as tab_err:
                logger.warning(f"Failed to open tab in existing browser: {tab_err}")
        return {
            "status": "already_running",
            "endpoint": f"http://127.0.0.1:{port}",
            "message": f"CloakBrowser is running. Opened tab for {open_url}." if open_url != "about:blank" else "CloakBrowser is connected and running with remote debugging enabled.",
        }

    exe = find_browser_executable()
    if not exe:
        raise RuntimeError("No suitable Chromium or CloakBrowser executable found on this system.")

    target_profile = profile_dir or DEFAULT_PROFILE_DIR
    target_profile.mkdir(parents=True, exist_ok=True)

    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={target_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--enable-logging=stderr",
        open_url,
    ]

    flags = 0
    if os.name == "nt":
        # Launch detached so the browser stays alive even after script finishes
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        args,
        close_fds=True,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait up to 8 seconds for CDP to become ready
    for _ in range(16):
        await asyncio.sleep(0.5)
        if await is_cdp_connected(port):
            return {
                "status": "launched",
                "pid": proc.pid,
                "executable": str(exe),
                "endpoint": f"http://127.0.0.1:{port}",
                "profile_dir": str(target_profile),
                "message": f"Successfully launched {exe.name} with persistent profile and CDP on port {port}.",
            }

    return {
        "status": "launching",
        "pid": proc.pid,
        "executable": str(exe),
        "endpoint": f"http://127.0.0.1:{port}",
        "profile_dir": str(target_profile),
        "message": "Browser started. Awaiting full initialization.",
    }
