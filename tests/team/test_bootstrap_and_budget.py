"""測試開機健檢、單一金鑰模型設定相容、安全設定匯出／匯入與每日 Token 成本護欄。"""
import json
import os
from unittest.mock import MagicMock, patch

from config import AppConfig
from team import bootstrap
from team.models import load_model_config


def test_preflight_missing_model_needs_setup_and_never_leaks_key(tmp_path):
    res = bootstrap.run_preflight_check(str(tmp_path))
    assert res["ok"] is True
    assert res["needs_setup"] is True
    assert res["model"]["ready"] is False
    assert isinstance(res["clis"], list) and len(res["clis"]) >= 3

    saved = bootstrap.save_model_config(
        str(tmp_path),
        {
            "base_url": "127.0.0.1:11434/v1",
            "key": "sk-secret-value-123456",
            "glm_model": "qwen2.5-coder:32b",
        },
    )
    assert saved["ok"] is True
    assert saved["base_url"] == "http://127.0.0.1:11434/v1"
    assert "sk-secret-value-123456" not in json.dumps(saved)

    # 驗證 load_model_config 單一 key（不填 decision1_key）也能載入
    cfg_path = os.path.join(str(tmp_path), "litellm.local.json")
    raw = json.loads(open(cfg_path, encoding="utf-8").read())
    del raw["decision1_key"]
    open(cfg_path, "w", encoding="utf-8").write(json.dumps(raw))
    mcfg = load_model_config(cfg_path)
    assert mcfg is not None
    assert mcfg.decision1_key == "sk-secret-value-123456"

    res2 = bootstrap.run_preflight_check(str(tmp_path))
    assert res2["model"]["ready"] is True
    assert "sk-secret-value-123456" not in json.dumps(res2)


def test_model_endpoint_probe_mock_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"data": [{"id": "glm-5.3"}, {"id": "Decision-1"}]}
    with patch("team.bootstrap.requests.get", return_value=mock_resp):
        out = bootstrap.test_model_endpoint("http://10.0.0.5:4000/v1", "sk-test", "glm-5.3")
    assert out["ok"] is True
    assert "glm-5.3" in out["models"]


def test_export_and_import_safe_profile_excludes_secrets(tmp_path):
    bootstrap.save_model_config(
        str(tmp_path),
        {"base_url": "http://10.0.0.5:4000/v1", "key": "sk-super-secret", "glm_model": "glm-5.3"},
    )
    cfg = AppConfig()
    cfg.team_daily_token_limit = 500000
    cfg.team_max_running = 6.0

    exported = bootstrap.export_safe_profile(cfg, str(tmp_path))
    dumped = json.dumps(exported)
    assert "sk-super-secret" not in dumped
    assert exported["team"]["team_daily_token_limit"] == 500000
    assert exported["team"]["team_max_running"] == 6

    cfg2 = AppConfig()
    cfg_file = os.path.join(str(tmp_path), "config.json")
    res = bootstrap.import_safe_profile(cfg2, cfg_file, str(tmp_path), exported)
    assert res["ok"] is True
    assert cfg2.team_daily_token_limit == 500000
    assert int(cfg2.team_max_running) == 6
