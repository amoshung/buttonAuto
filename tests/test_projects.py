import json
import os

from config import AppConfig
from projects import (
    ProjectDetector,
    WorkspaceEntry,
    load_workspaces,
    parse_folder_uri,
    parse_title,
    resolve_path,
    session_key,
)
import win_utils as wu


def test_parse_title_kiro_and_antigravity():
    assert parse_title("kiro", "Y6P2_Decision-1.md - buttonAuto - Kiro") == ("buttonAuto", "")
    assert parse_title("kiro", "● main.py - a - b - MyProj - Kiro") == ("MyProj", "")
    assert parse_title("kiro", "app.py - proj [SSH: host1] - Kiro") == ("proj", "host1")
    assert parse_title("kiro", "Getting started - Kiro") == ("Getting started", "")  # 由偵測器判斷是否真的是專案
    assert parse_title("kiro", "Kiro") == ("", "")
    assert parse_title("antigravity", "y6p2web2 - Antigravity IDE - Y6P2_Decision-1.md") == ("y6p2web2", "")
    assert parse_title("antigravity", "bofmgr [SSH: BOF456_NewServer] - Antigravity IDE - ref_master.rs") == ("bofmgr", "BOF456_NewServer")
    assert parse_title("antigravity", "Antigravity IDE") == ("", "")


def test_parse_folder_uri_local_and_remote():
    assert parse_folder_uri("file:///d%3A/%40BOF456%20Proj/MMI_Kernel_Origin") == (r"D:\@BOF456 Proj\MMI_Kernel_Origin", "")
    hexhost = "7b22686f73744e616d65223a22424f463435365f4e6577536572766572227d"  # {"hostName":"BOF456_NewServer"}
    assert parse_folder_uri(f"vscode-remote://ssh-remote%2B{hexhost}/home/bofmgr") == ("/home/bofmgr", "BOF456_NewServer")
    assert parse_folder_uri("vscode-remote://ssh-remote%2Bmyhost/srv/app") == ("/srv/app", "myhost")
    assert parse_folder_uri("untitled:Untitled-1") is None


def test_load_workspaces_and_resolve(tmp_path):
    base = tmp_path / "Kiro" / "User" / "workspaceStorage"
    for i, uri in enumerate(["file:///d%3A/A/proj", "file:///e%3A/B/proj", "file:///d%3A/other"]):
        d = base / f"ws{i}"
        d.mkdir(parents=True)
        (d / "workspace.json").write_text(json.dumps({"folder": uri}), encoding="utf-8")
        os.utime(d, (1000 + i, 1000 + i))
    (base / "broken").mkdir()
    (base / "broken" / "workspace.json").write_text("{not json", encoding="utf-8")

    ws = load_workspaces("Kiro", appdata=str(tmp_path))
    assert [w.path for w in ws][:2] == [r"D:\other", r"E:\B\proj"]  # 依最近使用排序
    path, amb = resolve_path("proj", "", ws)
    assert path == r"E:\B\proj" and amb is True
    assert resolve_path("other", "", ws) == (r"D:\other", False)
    assert resolve_path("missing", "", ws) == ("", False)
    assert resolve_path("proj", "somehost", ws) == ("", False)


def test_session_key_is_stable_and_case_insensitive():
    assert session_key("kiro", "buttonAuto", r"D:\_AI_Projs\buttonAuto", "") == r"kiro|d:\_ai_projs\buttonauto"
    assert session_key("antigravity", "bofmgr", "/home/bofmgr", "Host") == "antigravity|ssh://host/home/bofmgr"
    assert session_key("kiro", "x", "", "") == "kiro|x"


def test_detector_describe_and_unknown_welcome_page():
    det = ProjectDetector()
    det._cache["kiro"] = (1e18, [WorkspaceEntry("buttonAuto", r"D:\_AI_Projs\buttonAuto", "", 1.0)])
    pw = det.describe(wu.WindowInfo(1, "x.md - buttonAuto - Kiro", "Kiro.exe", False))
    assert pw.project == "buttonAuto" and pw.path == r"D:\_AI_Projs\buttonAuto" and pw.app_label == "Kiro"
    welcome = det.describe(wu.WindowInfo(2, "Getting started - Kiro", "Kiro.exe", False))
    assert welcome.project == ""
    assert det.describe(wu.WindowInfo(3, "Notepad", "notepad.exe", False)) is None


def test_config_authorization():
    cfg = AppConfig()
    assert not cfg.is_authorized("kiro|d:\\x")
    cfg.set_authorized("kiro|d:\\x", True)
    cfg.set_authorized("kiro|d:\\x", True)
    assert cfg.authorized_sessions == ["kiro|d:\\x"]
    cfg.set_authorized("kiro|d:\\x", False)
    assert cfg.authorized_sessions == []
