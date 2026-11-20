import os
import stat

import pytest

from team import clis


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    # 備援路徑從環境變數即時計算；指到空的 tmp，避免測試找到真的 CLI
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(home / "Local"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))


def _make_exe(directory, name):
    fname = name + (".exe" if os.name == "nt" else "")
    p = directory / fname
    p.write_text("")
    p.chmod(p.stat().st_mode | stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return p


def test_discover_finds_executables_on_given_path(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    for n in ("kiro-cli", "agy", "claude"):
        _make_exe(d, n)
    found = clis.discover(str(d))
    assert set(found) == {"kiro", "agy", "claude"}
    assert found["kiro"].protocol == "acp"
    assert found["agy"].protocol == "acp"
    assert found["claude"].protocol == "headless"
    assert found["kiro"].label == "Kiro"


def test_discover_skips_missing(tmp_path):
    d = tmp_path / "bin"
    d.mkdir()
    _make_exe(d, "agy")
    assert set(clis.discover(str(d))) == {"agy"}


def test_discover_uses_fallback_locations(tmp_path, monkeypatch):
    local = tmp_path / "fakehome" / "Local"
    (local / "agy" / "bin").mkdir(parents=True)
    exe = local / "agy" / "bin" / "agy.exe"
    exe.write_text("")
    exe.chmod(0o755)
    empty = tmp_path / "empty"
    empty.mkdir()
    found = clis.discover(str(empty))
    assert found["agy"].path == str(exe)


def test_launch_cmd():
    a = clis.CliInfo("kiro", "Kiro", "/x/kiro-cli", "acp")
    h = clis.CliInfo("claude", "Claude", "/x/claude", "headless")
    assert clis.launch_cmd(a) == ["/x/kiro-cli", "acp", "--agent-engine", "v3", "--auth-method", "cli"]
    assert clis.launch_cmd(clis.CliInfo("agy", "Antigravity", "/x/agy", "acp")) == ["/x/agy", "acp"]
    assert clis.launch_cmd(h) == ["/x/claude"]
