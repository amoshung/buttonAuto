"""Kiro v3（--agent-engine v3）：模型清單在 session/update 的 config_option_update，切換用 session/set_config_option。
假代理 FAKE_ACP_CONFIG=early|late 模擬（late＝session/new 回應後 0.5 秒才送）。"""
import json
import os
import sys

import pytest

from team.acp import AcpClient
from team.clis import CliInfo
from team.drivers import make_driver
from team.modelprefs import ModelPrefs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AGENT = os.path.join(ROOT, "team", "fakes", "fake_acp_agent.py")
PY = sys.executable
KIRO_IDS = ["auto", "claude-opus-5.5", "claude-sonnet-5.5", "claude-haiku-5.5", "gpt-5.6-sol", "gpt-5.6-terra",
            "gpt-5.6-luna", "grok-4.7", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4.5"]


@pytest.fixture
def v3(tmp_path, monkeypatch):
    def setup(mode):
        script = tmp_path / "s.json"
        script.write_text(json.dumps([[{"say": "hi"}]]), encoding="utf-8")
        monkeypatch.setenv("FAKE_ACP_MODELS", ",".join(KIRO_IDS))
        monkeypatch.setenv("FAKE_ACP_CONFIG", mode)
        monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.log"))
        return [PY, AGENT, str(script)]
    return setup


def _log(tmp_path):
    p = tmp_path / "acp.log"
    return p.read_text(encoding="utf-8").splitlines() if p.exists() else []


@pytest.mark.parametrize("mode", ["early", "late"])
def test_config_option_update_gives_models_and_set_config_option(tmp_path, v3, mode):
    c = AcpClient(v3(mode), str(tmp_path), lambda ev: None, lambda req: "")
    try:
        c.start()
        c.new_session()
        models = c.wait_models(3)
        assert [m["id"] for m in models] == KIRO_IDS and c.current_model == "auto"
        assert c.model_config_id == "model" and models[1]["name"] == "CLAUDE-OPUS-5.5"
        assert {o["id"] for o in c.config_options} == {"mode", "model"}
        assert c.set_model("claude-sonnet-5.5") and c.current_model == "claude-sonnet-5.5"
        assert not c.set_model("gpt-9")  # JSON-RPC 錯誤 → 失敗
        assert _log(tmp_path) == ["session/new", "set_config_option claude-sonnet-5.5", "set_config_option gpt-9"]
    finally:
        c.close()


def test_driver_waits_for_late_models_and_flags_failure(tmp_path, v3):
    cmd = v3("late")
    cli = CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp")
    ok = make_driver(cli, str(tmp_path), lambda ev: None, lambda req: "", False, cmd_override=cmd,
                     model="claude-haiku-5.5")
    bad = make_driver(cli, str(tmp_path), lambda ev: None, lambda req: "", False, cmd_override=cmd, model="gpt-9")
    try:
        ok.start()
        assert ok.model_failed is False and ok.client.current_model == "claude-haiku-5.5"
        bad.start()
        assert bad.model_failed is True  # 不在 configOptions 的清單：不送、交給問題流程
        assert "set_config_option gpt-9" not in _log(tmp_path)
    finally:
        ok.close()
        bad.close()


def test_office_detects_v3_models(tmp_path, v3, monkeypatch):
    from team import office as office_mod
    from team.fakes.fake_models import ScriptedRouter
    from team.office import Office
    from team.rooms import RoomStore
    cmd = v3("late")
    real = office_mod.make_driver
    monkeypatch.setattr(office_mod, "make_driver", lambda cli, cwd, *a, **k: real(cli, cwd, *a,
                                                                                 **dict(k, cmd_override=cmd)))
    store = RoomStore(str(tmp_path / "team"))
    # 舊版留下的「沒有回報可用模型」錯誤紀錄：讀取時丟掉，重新偵測會補上
    os.makedirs(store.root, exist_ok=True)
    with open(os.path.join(store.root, "models_cache.json"), "w", encoding="utf-8") as f:
        json.dump({"kiro": {"models": [], "error": "代理沒有回報可用模型（可能不支援選模型）"}}, f)
    o = Office(store, lambda demo: ScriptedRouter(), lambda: [], lambda: 1, lambda: None,
               clis_fn=lambda: {"kiro": CliInfo("kiro", "Kiro", "/nonexistent/kiro-cli", "acp")},
               base_dir=str(tmp_path), model_cfg=None, detect_on_start=False)
    k = o.models_view()["seats"]["kiro"]
    assert k["error"] == "" and k["known"] is False
    res = o._detect_seat("kiro")
    assert [m["id"] for m in res["models"]] == KIRO_IDS and res["current"] == "auto"
    assert "session/prompt" not in _log(tmp_path)
    o.prefs.note_detected("kiro", res["models"], res["current"])
    k = o.models_view()["seats"]["kiro"]
    assert k["known"] and len(k["models"]) == len(KIRO_IDS) and k["error"] == ""
    o.stop()


def test_stale_error_only_cache_dropped(tmp_path):
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"kiro": {"models": [], "error": "x"},
                                 "agy": {"models": [{"id": "gemini-3-pro"}], "current": "gemini-3-pro"}}),
                     encoding="utf-8")
    p = ModelPrefs(str(tmp_path / "m.json"), str(cache))
    assert p.available("kiro") is None and p.detected("kiro") == {}
    assert [m["id"] for m in p.available("agy")] == ["gemini-3-pro"]
