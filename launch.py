"""Start the local backend/frontend pair and open the web UI when ready."""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
import webbrowser

from start import BACKEND_PORT, FRONTEND_PORT, HOST, start


def _frontend_url() -> str:
    browser_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    return f"http://{browser_host}:{FRONTEND_PORT}/"


def _backend_url() -> str:
    browser_host = "127.0.0.1" if HOST in ("0.0.0.0", "::") else HOST
    return f"http://{browser_host}:{BACKEND_PORT}/docs"


def _wait_for_url(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.25)
    return False


if __name__ == "__main__":
    url = _frontend_url()
    backend_url = _backend_url()
    frontend_ready = _wait_for_url(url, timeout=0.5)
    backend_ready = _wait_for_url(backend_url, timeout=0.5)

    if frontend_ready and backend_ready:
        print("Yuketang Helper is already running")
    else:
        start()

    ready = _wait_for_url(url)
    if ready:
        print(f"  UI ready:   {url}")
    else:
        print(f"  UI is still starting; opening: {url}")
    webbrowser.open(url)
