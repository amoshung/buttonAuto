"""LiteLLM Proxy 線上模型與 USD/1M 定價每日自動掃描器（U11）。

- 資料來源：LiteLLM Proxy（預設 ``http://172.22.119.193:4000``）的 ``/v1/models`` 與 ``/model_group/info``。
- 價格單位：USD / 1,000,000 tokens；依 API 回傳的 ``input_cost_per_token`` 與
  ``output_cost_per_token`` 分別乘以 ``1,000,000`` 換算（缺值保留 ``None``，不誤填 0）。
- 目前設定作為預設種子；快取持久化至 ``sessions/team/litellm_catalog.json``，
  每日自動掃描一次（並支援手動觸發），代理離線時優雅保留上次快取與預設清單。
"""
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from team.models import _FORBIDDEN_MODELS, load_model_config

log = logging.getLogger(__name__)

DEFAULT_LITELLM_URL = "http://172.22.119.193:4000"
DAILY_TTL_SECONDS = 86400.0
TOKENS_PER_MILLION = 1_000_000.0


def _to_usd_per_1m(cost_per_token: Any) -> Optional[float]:
    if cost_per_token is None or cost_per_token == "":
        return None
    try:
        val = float(cost_per_token)
    except (TypeError, ValueError):
        return None
    if val < 0:
        return None
    return round(val * TOKENS_PER_MILLION, 6)


def _is_forbidden(model_id: str) -> bool:
    low = (model_id or "").lower()
    return any(bad in low for bad in _FORBIDDEN_MODELS)


def _default_seed_models(glm_model: str = "glm-5.3", d1_model: str = "Decision-1") -> List[Dict[str, Any]]:
    seeds = [
        {
            "id": glm_model or "glm-5.3",
            "label": glm_model or "glm-5.3",
            "provider": "litellm",
            "mode": "chat",
            "input_usd_per_1m": None,
            "output_usd_per_1m": None,
            "source": "default",
        },
        {
            "id": d1_model or "Decision-1",
            "label": d1_model or "Decision-1",
            "provider": "litellm",
            "mode": "systemone",
            "input_usd_per_1m": None,
            "output_usd_per_1m": None,
            "source": "default",
        },
    ]
    seen = set()
    out: List[Dict[str, Any]] = []
    for item in seeds:
        mid = item["id"]
        if mid and mid not in seen and not _is_forbidden(mid):
            seen.add(mid)
            out.append(item)
    return out


class ModelCatalogService:
    """每日掃描 LiteLLM Proxy 模型與 USD/1M 定價並落地快取。"""

    def __init__(
        self,
        base_dir: str,
        ttl_seconds: float = DAILY_TTL_SECONDS,
        timeout: float = 8.0,
        http_get: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._base_dir = base_dir
        self._ttl = float(ttl_seconds)
        self._timeout = float(timeout)
        self._get = http_get or requests.get
        self._now = clock or time.time
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._catalog_path = os.path.join(base_dir, "sessions", "team", "litellm_catalog.json")
        self._state: Dict[str, Any] = self._load_disk_or_seed()

    def _config_info(self) -> Dict[str, str]:
        cfg_path = os.path.join(self._base_dir, "litellm.local.json")
        cfg = load_model_config(cfg_path)
        if cfg is not None:
            return {
                "base_url": (cfg.base_url or DEFAULT_LITELLM_URL).rstrip("/"),
                "key": cfg.glm_key or "",
                "glm_model": cfg.glm_model or "glm-5.3",
                "d1_model": cfg.d1_model or "Decision-1",
            }
        return {
            "base_url": DEFAULT_LITELLM_URL,
            "key": "",
            "glm_model": "glm-5.3",
            "d1_model": "Decision-1",
        }

    def _load_disk_or_seed(self) -> Dict[str, Any]:
        info = self._config_info()
        seed = _default_seed_models(info["glm_model"], info["d1_model"])
        if os.path.isfile(self._catalog_path):
            try:
                with open(self._catalog_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("models"), list):
                    return data
            except (OSError, ValueError):
                pass
        return {
            "source_url": info["base_url"],
            "price_unit": "USD / 1,000,000 tokens",
            "last_scanned_at": None,
            "degraded": False,
            "error": "",
            "models": seed,
        }

    def _save_disk(self, state: Dict[str, Any]) -> None:
        parent = os.path.dirname(self._catalog_path)
        os.makedirs(parent, exist_ok=True)
        tmp = self._catalog_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._catalog_path)

    def _headers(self, key: str) -> Dict[str, str]:
        if key and key != "local-no-key":
            return {"Authorization": f"Bearer {key}"}
        return {}

    def _fetch_endpoint(self, url: str, headers: Dict[str, str]) -> Any:
        resp = self._get(url, headers=headers, timeout=self._timeout)
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(f"{url} 回傳 HTTP {getattr(resp, 'status_code', 0)}")
        return resp.json()

    def scan_now(self) -> Dict[str, Any]:
        """立即向 LiteLLM Proxy 掃描 /v1/models 與 /model_group/info 並更新快取。"""
        info = self._config_info()
        base_url = info["base_url"].rstrip("/")
        # 若 base_url 以 /v1 結尾，取根位址以便同時打 /v1/models 與 /model_group/info
        root_url = base_url[:-3] if base_url.endswith("/v1") else base_url
        headers = self._headers(info["key"])
        now = self._now()

        try:
            models_payload = self._fetch_endpoint(f"{root_url}/v1/models", headers)
            raw_models = models_payload.get("data", []) if isinstance(models_payload, dict) else []
            discovered_ids: List[str] = []
            for item in raw_models:
                if isinstance(item, dict) and item.get("id"):
                    mid = str(item["id"]).strip()
                    if mid and not _is_forbidden(mid):
                        discovered_ids.append(mid)

            group_info_map: Dict[str, Dict[str, Any]] = {}
            group_error = ""
            try:
                group_payload = self._fetch_endpoint(f"{root_url}/model_group/info", headers)
                raw_groups = group_payload.get("data", []) if isinstance(group_payload, dict) else []
                for g in raw_groups:
                    if not isinstance(g, dict):
                        continue
                    gid = str(g.get("model_group") or g.get("id") or "").strip()
                    if not gid or _is_forbidden(gid):
                        continue
                    group_info_map[gid] = g
                    if gid not in discovered_ids:
                        discovered_ids.append(gid)
            except Exception as exc:
                group_error = str(exc)

            merged_by_id: Dict[str, Dict[str, Any]] = {}
            for mid in discovered_ids:
                g = group_info_map.get(mid, {})
                in_usd = _to_usd_per_1m(g.get("input_cost_per_token"))
                out_usd = _to_usd_per_1m(g.get("output_cost_per_token"))
                merged_by_id[mid] = {
                    "id": mid,
                    "label": mid,
                    "provider": str(g.get("providers")[0]) if isinstance(g.get("providers"), list) and g.get("providers") else "litellm",
                    "mode": str(g.get("mode") or "chat"),
                    "max_input_tokens": g.get("max_input_tokens"),
                    "max_output_tokens": g.get("max_output_tokens"),
                    "input_usd_per_1m": in_usd,
                    "output_usd_per_1m": out_usd,
                    "source": "online",
                }

            # 確保預設種子模型保留（若線上沒回傳才補入）
            for seed in _default_seed_models(info["glm_model"], info["d1_model"]):
                if seed["id"] not in merged_by_id:
                    merged_by_id[seed["id"]] = seed

            new_state: Dict[str, Any] = {
                "source_url": root_url,
                "price_unit": "USD / 1,000,000 tokens",
                "last_scanned_at": int(now),
                "degraded": bool(group_error),
                "error": f"/model_group/info 無法取得：{group_error[:160]}" if group_error else "",
                "models": list(merged_by_id.values()),
            }
            with self._lock:
                self._state = new_state
            self._save_disk(new_state)
            return self.snapshot()
        except Exception as exc:
            log.warning("LiteLLM 模型掃描失敗，保留快取與預設清單：%s", exc)
            with self._lock:
                fallback = dict(self._state)
                fallback["source_url"] = root_url
                fallback["degraded"] = True
                fallback["error"] = f"線上掃描失敗（保留上次快取與預設）：{str(exc)[:180]}"
                if not fallback.get("models"):
                    fallback["models"] = _default_seed_models(info["glm_model"], info["d1_model"])
                self._state = fallback
            return self.snapshot()

    def is_stale(self) -> bool:
        with self._lock:
            last = self._state.get("last_scanned_at")
        if not last:
            return True
        return (self._now() - float(last)) >= self._ttl

    def ensure_fresh_if_stale(self) -> bool:
        """若快取超過 24 小時則執行掃描，回傳是否有觸發掃描。"""
        if self.is_stale():
            self.scan_now()
            return True
        return False

    def start_daily_scheduler(self) -> None:
        """啟動每日背景自動掃描執行緒（非阻塞）。"""
        if self._thread is not None and self._thread.is_alive():
            return

        def _loop() -> None:
            try:
                self.ensure_fresh_if_stale()
            except Exception:
                pass
            while not self._stop_event.wait(3600.0):
                try:
                    self.ensure_fresh_if_stale()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_loop, name="litellm-catalog-daily", daemon=True)
        self._thread.start()

    def stop_scheduler(self) -> None:
        self._stop_event.set()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._state)
            snap["models"] = [dict(m) for m in self._state.get("models", [])]
            return snap

    def get_pricing(self, model_id: str) -> Optional[Dict[str, Optional[float]]]:
        """依模型名稱查詢 USD / 1M tokens 定價；查無則回 None。"""
        target = (model_id or "").strip()
        if not target:
            return None
        with self._lock:
            for m in self._state.get("models", []):
                if m.get("id") == target:
                    return {
                        "input_usd_per_1m": m.get("input_usd_per_1m"),
                        "output_usd_per_1m": m.get("output_usd_per_1m"),
                    }
        return None

    def estimate_cost_usd(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        """以掃描到的 USD / 1M tokens 定價估算單次呼叫費用（USD）。"""
        pricing = self.get_pricing(model_id)
        if not pricing:
            return None
        in_p = pricing.get("input_usd_per_1m")
        out_p = pricing.get("output_usd_per_1m")
        if in_p is None and out_p is None:
            return None
        cost = ((prompt_tokens or 0) * (in_p or 0.0) + (completion_tokens or 0) * (out_p or 0.0)) / TOKENS_PER_MILLION
        return round(cost, 6)


_NON_CHAT_TOKENS = ("transcribe", "embed", "rerank", "whisper", "tts", "dall-e", "decision-1")
_STRONG_HINTS = ("opus", "gemini-4", "pro", "sonnet", "gpt-5", "gpt-6", "grok-4", "glm-5")


def _load_paid_litellm_ids(store_root: str) -> set[str]:
    paid: set[str] = set()
    path = os.path.join(store_root, "litellm_catalog.json")
    if not os.path.isfile(path):
        return paid
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for m in data.get("models") or []:
            mid = str(m.get("id") or "").strip()
            in_p = float(m.get("input_usd_per_1m") or 0.0)
            out_p = float(m.get("output_usd_per_1m") or 0.0)
            if mid and (in_p > 0.0 or out_p > 0.0):
                paid.add(mid)
    except Exception:
        pass
    return paid


def select_strongest_planner(
    clis: Dict[str, Any],
    *,
    store_root: str = "sessions/team",
    model_cfg: Any = None,
    d1_fn: Any = None,
) -> Tuple[str, str, str]:
    """排除 LiteLLM 付費模型後，透過 Decision-1 從可用座位／模型中選出最強的計畫生成模型。

    回傳 ``(seat, model, reason)``。
    """
    paid_ids = _load_paid_litellm_ids(store_root)
    cache_path = os.path.join(store_root, "models_cache.json")
    cached: Dict[str, Any] = {}
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
        except Exception:
            cached = {}

    candidates: List[Tuple[str, str]] = []
    seen: set[Tuple[str, str]] = set()

    def _add(seat: str, mid: str) -> None:
        m = (mid or "").strip()
        if not m or m.lower() == "auto":
            return
        if any(tok in m.lower() for tok in _NON_CHAT_TOKENS):
            return
        # 嚴格排除 LiteLLM 付費模型（claude/litellm 清單或直呼 LiteLLM 皆排除；kiro/agy 為獨立訂閱 CLI）
        if seat not in ("kiro", "agy") and m in paid_ids:
            return
        if (seat, m) not in seen:
            seen.add((seat, m))
            candidates.append((seat, m))

    for seat in ("kiro", "agy", "claude"):
        if seat not in (clis or {}):
            continue
        models_list = ((cached.get(seat) or {}).get("models")) or []
        if not models_list:
            # 沒有快取時補上該 CLI 預設旗艦別名
            default_m = {"kiro": "claude-opus-5.5", "agy": "gemini-4-hydrogen", "claude": "opus"}[seat]
            _add(seat, default_m)
        else:
            for item in models_list:
                mid = str(item.get("id") or "")
                # claude 清單只保留原生/免費模型，過濾掉額外掛進來的第三方付費模型
                if seat == "claude" and mid in paid_ids:
                    continue
                _add(seat, mid)

    if model_cfg is not None:
        glm_m = str(getattr(model_cfg, "glm_model", "") or "glm-5.3").strip()
        if glm_m and glm_m not in paid_ids:
            _add("glm", glm_m)

    if not candidates:
        return "glm", (getattr(model_cfg, "glm_model", "glm-5.3") if model_cfg else "glm-5.3"), "fallback_default"

    # 先依旗艦關鍵字精選最多 16 個候選交給 Decision-1 單次評分
    ranked_pool = sorted(
        candidates,
        key=lambda sm: (
            0 if any(h in sm[1].lower() for h in _STRONG_HINTS) else 1,
            0 if sm[0] in ("kiro", "agy") else 1,
        ),
    )[:16]

    if d1_fn is not None or (model_cfg is not None and getattr(model_cfg, "decision1_key", "")):
        try:
            if d1_fn is not None:
                answers = d1_fn(
                    "候選免費/訂閱制規劃模型：\n" + "\n".join(f"- {s}/{m}" for s, m in ranked_pool),
                    {
                        f"c{i:02d}_strongest": {
                            "type": "noul",
                            "instructions": f"在複雜軟體架構設計、跨檔案需求拆解與計畫生成任務上，模型 {s}/{m} 的綜合規劃與推理能力是否屬於最頂尖旗艦等級？",
                        }
                        for i, (s, m) in enumerate(ranked_pool)
                    },
                )
            else:
                from ai_client import DecisionClient
                client = DecisionClient(base_url=model_cfg.base_url, api_key=model_cfg.decision1_key, timeout=8.0)
                answers = client.systemone(
                    "候選免費/訂閱制規劃模型（已排除 LiteLLM 付費模型）：\n" + "\n".join(f"- {s}/{m}" for s, m in ranked_pool),
                    {
                        f"c{i:02d}_strongest": {
                            "type": "noul",
                            "instructions": f"在複雜軟體架構設計、跨檔案需求拆解與計畫生成任務上，模型 {s}/{m} 的綜合規劃與推理能力是否屬於最頂尖旗艦等級？",
                        }
                        for i, (s, m) in enumerate(ranked_pool)
                    },
                )
            if isinstance(answers, dict) and answers:
                best_idx = 0
                best_score = -1.0
                for i in range(len(ranked_pool)):
                    sc = float((answers.get(f"c{i:02d}_strongest") or {}).get("noul", 0.0))
                    if sc > best_score:
                        best_score = sc
                        best_idx = i
                seat, mid = ranked_pool[best_idx]
                return seat, mid, f"d1_selected(score={best_score:.2f})"
        except Exception:
            pass

    seat, mid = ranked_pool[0]
    return seat, mid, "heuristic_strongest"
