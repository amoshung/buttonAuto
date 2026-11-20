"""U11：team/model_catalog.py LiteLLM Proxy 模型與 USD/1M 定價每日自動掃描測試。"""
import json
import os

from team.model_catalog import DEFAULT_LITELLM_URL, ModelCatalogService


class _FakeResp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def test_catalog_scans_models_and_converts_usd_per_1m(tmp_path):
    cfg_path = tmp_path / "litellm.local.json"
    cfg_path.write_text(
        json.dumps({"base_url": DEFAULT_LITELLM_URL, "key": "sk-test-123456"}),
        encoding="utf-8",
    )

    calls = []

    def fake_get(url, headers=None, timeout=8.0):
        calls.append((url, headers))
        if url.endswith("/v1/models"):
            return _FakeResp(
                200,
                {
                    "data": [
                        {"id": "glm-5.3"},
                        {"id": "gpt-5.1"},
                        {"id": "claude-sonnet-4"},
                        {"id": "gemma-2b"},  # 禁用模型應被過濾
                    ]
                },
            )
        if url.endswith("/model_group/info"):
            return _FakeResp(
                200,
                {
                    "data": [
                        {
                            "model_group": "glm-5.3",
                            "providers": ["openai"],
                            "mode": "chat",
                            "input_cost_per_token": 0.0000015,
                            "output_cost_per_token": 0.000006,
                        },
                        {
                            "model_group": "gpt-5.1",
                            "providers": ["openai"],
                            "mode": "chat",
                            "input_cost_per_token": 0.000002,
                            "output_cost_per_token": 0.000008,
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected url: {url}")

    now = [1_700_000_000.0]
    svc = ModelCatalogService(str(tmp_path), ttl_seconds=86400, http_get=fake_get, clock=lambda: now[0])
    snap = svc.scan_now()

    assert snap["degraded"] is False
    assert snap["price_unit"] == "USD / 1,000,000 tokens"
    assert snap["source_url"] == DEFAULT_LITELLM_URL
    by_id = {m["id"]: m for m in snap["models"]}
    assert "gemma-2b" not in by_id
    assert by_id["glm-5.3"]["input_usd_per_1m"] == 1.5
    assert by_id["glm-5.3"]["output_usd_per_1m"] == 6.0
    assert by_id["gpt-5.1"]["input_usd_per_1m"] == 2.0
    assert by_id["gpt-5.1"]["output_usd_per_1m"] == 8.0
    # /model_group/info 未提供價格的模型不可偽造為 0
    assert by_id["claude-sonnet-4"]["input_usd_per_1m"] is None
    assert by_id["claude-sonnet-4"]["output_usd_per_1m"] is None

    # 估算費用：(100_000 * 1.5 + 50_000 * 6.0) / 1_000_000 = 0.45 USD
    assert svc.estimate_cost_usd("glm-5.3", 100_000, 50_000) == 0.45
    assert svc.estimate_cost_usd("claude-sonnet-4", 100_000, 50_000) is None

    # 快取落地且 24 小時內不重複掃描
    cache_file = tmp_path / "sessions" / "team" / "litellm_catalog.json"
    assert os.path.isfile(cache_file)
    assert svc.ensure_fresh_if_stale() is False
    assert len(calls) == 2

    # 超過 24 小時自動重掃
    now[0] += 86401.0
    assert svc.ensure_fresh_if_stale() is True
    assert len(calls) == 4


def test_catalog_offline_preserves_defaults_and_prior_cache(tmp_path):
    def fail_get(url, headers=None, timeout=8.0):
        raise ConnectionError("proxy unreachable")

    svc = ModelCatalogService(str(tmp_path), http_get=fail_get)
    snap = svc.scan_now()
    assert snap["degraded"] is True
    ids = {m["id"] for m in snap["models"]}
    assert "glm-5.3" in ids
    assert "Decision-1" in ids
