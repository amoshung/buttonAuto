from config import AppConfig
from clicker import ClickController


def test_config_default_and_persistence(tmp_path):
    cfg_file = tmp_path / "config.json"
    cfg = AppConfig.load(str(cfg_file))
    assert cfg.scan_interval == 1.0
    assert cfg.threshold == 0.85
    assert len(cfg.targets) == 2
    assert cfg.regions == []

    cfg.scan_interval = 0.5
    cfg.regions.append({"x": 10, "y": 20, "w": 300, "h": 200, "enabled": True})
    cfg.save(str(cfg_file))

    reloaded = AppConfig.load(str(cfg_file))
    assert reloaded.scan_interval == 0.5
    assert len(reloaded.regions) == 1
    assert reloaded.regions[0]["w"] == 300


def test_click_controller_cooldown_and_cursor_restore(monkeypatch):
    calls = []
    fake_pos = [500, 600]

    def mock_get_pos():
        return (fake_pos[0], fake_pos[1])

    def mock_set_pos(x, y):
        calls.append(("move", x, y))
        fake_pos[0], fake_pos[1] = x, y

    def mock_mouse_event(flags, dx=0, dy=0, data=0, extra=0):
        calls.append(("event", flags))

    monkeypatch.setattr("clicker.get_cursor_pos", mock_get_pos)
    monkeypatch.setattr("clicker.set_cursor_pos", mock_set_pos)
    monkeypatch.setattr("clicker._send_mouse_event", mock_mouse_event)

    ctrl = ClickController()
    clicked = ctrl.click_and_restore(100, 200, cooldown_sec=0.5)
    assert clicked is True
    assert calls[0] == ("move", 100, 200)
    assert calls[-1] == ("move", 500, 600)

    # Immediate second click at same position should be blocked by cooldown
    clicked_again = ctrl.click_and_restore(102, 198, cooldown_sec=0.5)
    assert clicked_again is False

