"""Finding Brave and Chrome on each platform, without those platforms."""

from __future__ import annotations

from pathlib import Path

import pytest

from browseruse.browser import launcher
from browseruse.browser.launcher import BrowserLaunchError, default_user_data_dir, find_browser


@pytest.fixture
def fake_fs(monkeypatch, tmp_path):
    """Pretend a set of paths exists, and nothing else."""
    present: set[str] = set()
    real_exists = Path.exists

    def exists(self):
        return str(self) in present or real_exists(self)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: None)
    return present


def test_env_override_wins(monkeypatch, tmp_path):
    executable = tmp_path / "my-brave"
    executable.write_text("")
    monkeypatch.setenv("BROWSERUSE_BRAVE_PATH", str(executable))
    assert find_browser("brave") == executable


def test_a_bad_env_override_says_so_rather_than_falling_back(monkeypatch):
    monkeypatch.setenv("BROWSERUSE_CHROME_PATH", "/nope/chrome")
    with pytest.raises(BrowserLaunchError, match="does not exist"):
        find_browser("chrome")


@pytest.mark.parametrize(
    ("browser", "path"),
    [
        ("brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
        ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ],
)
def test_macos_application_paths(monkeypatch, fake_fs, browser, path):
    monkeypatch.delenv(f"BROWSERUSE_{browser.upper()}_PATH", raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    fake_fs.add(path)
    assert str(find_browser(browser)) == path


@pytest.mark.parametrize(
    ("browser", "relative"),
    [
        ("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("chrome", r"Google\Chrome\Application\chrome.exe"),
    ],
)
def test_windows_program_files_and_localappdata(monkeypatch, fake_fs, browser, relative):
    monkeypatch.delenv(f"BROWSERUSE_{browser.upper()}_PATH", raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Windows")
    monkeypatch.setenv("PROGRAMFILES", r"C:\Program Files")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\me\AppData\Local")

    # Only the per-user install exists: the search must reach LOCALAPPDATA.
    fake_fs.add(str(Path(r"C:\Users\me\AppData\Local") / relative))
    found = find_browser(browser)
    assert "AppData" in str(found)


def test_a_missing_browser_names_the_override_env_var(monkeypatch, fake_fs):
    monkeypatch.delenv("BROWSERUSE_BRAVE_PATH", raising=False)
    monkeypatch.setattr(launcher.platform, "system", lambda: "Darwin")
    with pytest.raises(BrowserLaunchError, match="BROWSERUSE_BRAVE_PATH"):
        find_browser("brave")


@pytest.mark.parametrize(
    ("system", "browser", "fragment"),
    [
        ("Darwin", "brave", "BraveSoftware/Brave-Browser"),
        ("Darwin", "chrome", "Google/Chrome"),
        ("Windows", "brave", "Brave-Browser"),
        ("Windows", "chrome", "Chrome"),
        ("Linux", "brave", "Brave-Browser"),
        ("Linux", "chrome", "google-chrome"),
    ],
)
def test_real_profile_directories_per_platform(monkeypatch, system, browser, fragment):
    monkeypatch.setattr(launcher.platform, "system", lambda: system)
    assert fragment in str(default_user_data_dir(browser))


def test_cdp_endpoint_returns_none_when_nothing_is_listening():
    # Port 1 is never a DevTools endpoint.
    assert launcher.cdp_endpoint(1, timeout=0.2) is None
