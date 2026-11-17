"""背景輸入（不碰鍵盤與焦點）、等 Allow 時的互卡修正、自主程度預設值。不碰任何 IDE。"""
import queue

import pytest

import automation
import uia_driver as ud
from ai_settings import AISettings
from config import AUTONOMY_PRESETS, AppConfig


class FakeInput:
    """模擬聊天輸入框：accept=False 代表編輯器不接受背景放入的文字（送出鈕維持停用）。"""

    def __init__(self, accept=True, clears_on_send=True, can_clear=True):
        self.text = ""
        self.accept = accept
        self.clears_on_send = clears_on_send
        self.can_clear = can_clear
        self.sent = []


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    from sessions import SessionStore
    monkeypatch.setattr(automation, "SessionStore", lambda: SessionStore(root=str(tmp_path / "s")))
    monkeypatch.setattr(automation.time, "sleep", lambda *_: None)
    return automation.AutomationWorker(AppConfig(), lambda: AISettings(), queue.Queue())


def _wire(monkeypatch, inp):
    def set_value(c, text):
        if text == "" and not c.can_clear:
            return True
        c.text = text
        return True
    monkeypatch.setattr(ud, "set_value", set_value)
    monkeypatch.setattr(ud, "input_text", lambda c: c.text)
    monkeypatch.setattr(ud, "find_send_button", lambda c: object() if (c.accept and c.text) else None)

    def press(_b):
        inp.sent.append(inp.text)
        if inp.clears_on_send:
            inp.text = ""
        return True
    monkeypatch.setattr(ud, "press", press)


def test_background_send_success(worker, monkeypatch):
    inp = FakeInput()
    _wire(monkeypatch, inp)
    assert worker._background_send(inp, "繼續") == ("sent", "")
    assert inp.sent == ["繼續"] and inp.text == ""


def test_background_send_falls_back_when_editor_ignores_it(worker, monkeypatch):
    inp = FakeInput(accept=False)
    _wire(monkeypatch, inp)
    result, why = worker._background_send(inp, "繼續")
    assert result == "fallback" and "沒有接受" in why and inp.text == "" and inp.sent == []


def test_background_send_fails_hard_if_text_is_stuck(worker, monkeypatch):
    inp = FakeInput(clears_on_send=False, can_clear=False)
    _wire(monkeypatch, inp)
    result, why = worker._background_send(inp, "繼續")
    assert result == "failed" and "手動清空" in why  # 不能再改用前景輸入，否則會變成兩份


def test_background_send_without_value_pattern(worker, monkeypatch):
    monkeypatch.setattr(ud, "set_value", lambda c, t: False)
    assert worker._background_send(object(), "x")[0] == "fallback"


def test_background_ok_respects_user_in_same_window(worker, monkeypatch):
    class W:
        hwnd = 5
    monkeypatch.setattr(automation.wu, "get_foreground", lambda: 5)
    monkeypatch.setattr(automation.wu, "real_idle_seconds", lambda: 0.2)
    assert worker._background_ok(W()) is False  # 你正在用這個 IDE：不動它的聊天框
    assert worker._background_ok(W(), by="user") is True
    monkeypatch.setattr(automation.wu, "get_foreground", lambda: 99)
    assert worker._background_ok(W()) is True  # 你在別的視窗打字：照送
    worker.cfg.ai_background_input = False
    assert worker._background_ok(W()) is False


def test_poke_resets_fingerprint(worker):
    worker._hwnd_of["k"] = 7
    st = worker.states.setdefault(7, automation.WinState(last_fp_text="abc", last_analysis=99.0, cooldown_until=1e12))
    worker.poke("k")
    assert st.last_fp_text == "" and st.last_analysis == 0.0 and st.cooldown_until == 0.0


def test_autonomy_presets_round_trip(tmp_path):
    cfg = AppConfig()
    assert cfg.apply_autonomy("careful") and cfg.ai_max_risk == AUTONOMY_PRESETS["careful"]["values"]["ai_max_risk"]
    assert not cfg.apply_autonomy("reckless") and cfg.autonomy_level == "careful"
    p = tmp_path / "c.json"
    cfg.save(str(p))
    again = AppConfig.load(str(p))
    assert again.autonomy_level == "careful" and again.ai_background_input is True and again.web_port == 8765.0
    for preset in AUTONOMY_PRESETS.values():  # 任何程度都不會把風險門檻開到「全部自動」
        assert preset["values"]["ai_max_risk"] < 0.5 and preset["values"]["ai_risk_review_max"] < 0.9
