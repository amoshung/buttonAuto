"""測試不寫入助理真正的 automation.log、不跳 Windows 通知（避免混進你看的日誌與通知）。"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_side_effects(tmp_path, monkeypatch):
    import automation
    import win_utils
    monkeypatch.setattr(automation, "LOG_PATH", str(tmp_path / "automation.log"))
    monkeypatch.setattr(win_utils, "notify", lambda *a, **k: True)
