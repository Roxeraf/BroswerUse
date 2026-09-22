"""Find Brave/Chrome on this machine and get a CDP endpoint we can attach to.

The point of attaching over CDP rather than letting Playwright download its own
Chromium is that it has to be *your* browser, with your logins, your extensions
and your bookmarks. That only works if the browser is started with
``--remote-debugging-port``, which is why this module would rather launch the
browser itself than find one already running without the flag.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from browseruse.config import BrowserName, Config


class BrowserLaunchError(RuntimeError):
    """Raised when no browser could be found, launched, or attached to."""


# Ordered best-guess locations per platform. The first hit wins.
_MACOS_PATHS: dict[BrowserName, list[str]] = {
    "brave": [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Brave Browser Beta.app/Contents/MacOS/Brave Browser Beta",
    ],
    "chrome": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
    ],
}

_WINDOWS_RELATIVE: dict[BrowserName, list[str]] = {
    "brave": [r"BraveSoftware\Brave-Browser\Application\brave.exe"],
    "chrome": [r"Google\Chrome\Application\chrome.exe"],
}

_LINUX_COMMANDS: dict[BrowserName, list[str]] = {
    "brave": ["brave-browser", "brave", "brave-browser-stable"],
    "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
}


def _windows_roots() -> list[str]:
    """The three places Chromium browsers install themselves on Windows."""
    return [
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")),
    ]


def find_browser(browser: BrowserName) -> Path:
    """Locate the executable for ``browser``, or raise ``BrowserLaunchError``."""
    override = os.getenv(f"BROWSERUSE_{browser.upper()}_PATH")
    if override:
        candidate = Path(override).expanduser()
        if candidate.exists():
            return candidate
        raise BrowserLaunchError(
            f"BROWSERUSE_{browser.upper()}_PATH points at {candidate}, which does not exist."
        )

    system = platform.system()
    if system == "Darwin":
        for raw in _MACOS_PATHS[browser]:
            candidate = Path(raw).expanduser()
            if candidate.exists():
                return candidate
    elif system == "Windows":
        for root in _windows_roots():
            for relative in _WINDOWS_RELATIVE[browser]:
                candidate = Path(root) / relative
                if candidate.exists():
                    return candidate
    else:
        for command in _LINUX_COMMANDS[browser]:
            found = shutil.which(command)
            if found:
                return Path(found)

    raise BrowserLaunchError(
        f"Could not find {browser} on this {system} machine. "
        f"Install it, or set BROWSERUSE_{browser.upper()}_PATH to the executable."
    )


def default_user_data_dir(browser: BrowserName) -> Path:
    """The real, everyday profile directory for ``browser`` on this machine."""
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        base = home / "Library" / "Application Support"
        return base / ("BraveSoftware/Brave-Browser" if browser == "brave" else "Google/Chrome")
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        relative = (
            r"BraveSoftware\Brave-Browser\User Data"
            if browser == "brave"
            else r"Google\Chrome\User Data"
        )
        return base / relative
    base = home / ".config"
    return base / ("BraveSoftware/Brave-Browser" if browser == "brave" else "google-chrome")


def cdp_endpoint(port: int, timeout: float = 1.0) -> str | None:
    """Return the websocket URL if something is already listening on ``port``."""
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - localhost only
            payload = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    return payload.get("webSocketDebuggerUrl")


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


@dataclass
class LaunchedBrowser:
    """A CDP endpoint plus the process behind it, when we started it ourselves."""

    ws_endpoint: str
    port: int
    process: subprocess.Popen[bytes] | None

    def terminate(self) -> None:
        """Close the browser only if this process launched it."""
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


def _stderr_tail(handle: Any, lines: int = 6) -> str:
    """The last few lines the browser printed, for the error message."""
    try:
        handle.seek(0)
        text = handle.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""
    if not text:
        return ""
    tail = "\n".join(text.splitlines()[-lines:])
    return f"\nThe browser said:\n{tail}"


def launch(config: Config, *, wait_seconds: float = 25.0) -> LaunchedBrowser:
    """Attach to a browser on ``config.cdp_port``, starting one if needed."""
    existing = cdp_endpoint(config.cdp_port)
    if existing:
        return LaunchedBrowser(ws_endpoint=existing, port=config.cdp_port, process=None)

    executable = find_browser(config.browser)
    user_data_dir = (
        default_user_data_dir(config.browser) if config.real_profile else config.profile_dir
    )
    user_data_dir.mkdir(parents=True, exist_ok=True)

    args = [
        str(executable),
        f"--remote-debugging-port={config.cdp_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        # Brave's rewards/wallet prompts steal focus from a fresh profile.
        "--disable-brave-update",
    ]
    if config.headless:
        args.append("--headless=new")
    if platform.system() == "Linux" and os.geteuid() == 0:
        # Chromium refuses to start as root with its sandbox on. This only ever
        # applies to containers and CI; a normal desktop user never hits it.
        args.append("--no-sandbox")

    # Keep stderr: when Chromium refuses to start it says why, and that message
    # is far more useful than anything we could guess.
    stderr_file = tempfile.TemporaryFile()
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=stderr_file)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        endpoint = cdp_endpoint(config.cdp_port)
        if endpoint:
            return LaunchedBrowser(endpoint, config.cdp_port, process)
        if process.poll() is not None:
            raise BrowserLaunchError(
                f"{config.browser.title()} exited instead of opening a debugging port.\n"
                f"The usual cause is that it is already running with this profile "
                f"({user_data_dir}): Chromium hands the command line to the running "
                f"instance and exits, and that instance has no debugging port. "
                f"Quit it completely and try again.\n"
                + _stderr_tail(stderr_file)
            )
        time.sleep(0.25)

    process.terminate()
    hint = "" if _port_is_free(config.cdp_port) else f" Something else is using port {config.cdp_port}."
    raise BrowserLaunchError(
        f"{config.browser.title()} did not expose a debugging port within "
        f"{wait_seconds:.0f}s.{hint}\n" + _stderr_tail(stderr_file)
    )
