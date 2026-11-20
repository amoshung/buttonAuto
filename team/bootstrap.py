"""開機健檢、模型端點連線測試與安全設定檔匯出／匯入。

提供首次啟動 Onboarding Wizard 與設定同步所需的後端能力：
- run_preflight_check：偵測本機 CLI（claude / agy / kiro-cli）、git 與 litellm.local.json 狀態（絕不回傳明文金鑰）。
- test_model_endpoint：對使用者輸入的 Base URL（含 IP:Port）發送輕量 /models 探測。
- save_model_config：將 Base URL、模型名稱與金鑰安全寫入 litellm.local.json。
- export_safe_profile / import_safe_profile：匯出與匯入不含任何金鑰的團隊與模型名稱設定。
"""
import json
import os
import shutil
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from team import clis
from team.models import load_model_config

DEFAULT_LITELLM_URL = "http://172.22.119.193:4000"

CLI_INSTALL_HINTS: Dict[str, Dict[str, str]] = {
    "claude": {
        "label": "Claude Code CLI",
        "command": "npm install -g @anthropic-ai/claude-code",
        "url": "https://docs.anthropic.com/en/docs/claude-code",
        "note": "安裝後請於終端機執行 claude 完成登入。",
    },
    "agy": {
        "label": "Antigravity CLI (agy)",
        "command": "winget install Google.Antigravity",
        "url": "https://antigravity.google",
        "note": "安裝後請確認 agy 在 PATH 中並已登入帳號。",
    },
    "kiro": {
        "label": "Kiro CLI",
        "command": "irm https://kiro.dev/install.ps1 | iex",
        "url": "https://kiro.dev",
        "note": "安裝後請於終端機執行 kiro-cli login 完成登入。",
    },
}


def _litellm_path(base_dir: str) -> str:
    return os.path.join(base_dir, "litellm.local.json")


def _mask_key(val: str) -> str:
    v = (val or "").strip()
    if not v:
        return ""
    if len(v) <= 6:
        return "***"
    return f"{v[:3]}***{v[-3:]}"


def run_preflight_check(base_dir: str, verify_live: bool = True) -> Dict[str, Any]:
    """檢查本機 CLI、Git 與模型設定狀態，並於設定就緒時主動驗證 API 金鑰有效性（絕不含明文金鑰）。"""
    found = clis.discover()
    cli_items: List[Dict[str, Any]] = []
    for name, (label, exe, protocol) in clis.CLI_SPECS.items():
        info = found.get(name)
        hint = CLI_INSTALL_HINTS.get(name, {})
        cli_items.append({
            "name": name,
            "label": hint.get("label") or label,
            "exe": exe,
            "protocol": protocol,
            "installed": info is not None,
            "path": info.path if info else "",
            "install_command": hint.get("command", ""),
            "docs_url": hint.get("url", ""),
            "note": hint.get("note", ""),
        })

    git_path = shutil.which("git") or ""
    cfg_path = _litellm_path(base_dir)
    model_cfg = load_model_config(cfg_path)
    raw_meta: Dict[str, Any] = {}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                raw_meta = loaded
        except (OSError, ValueError):
            raw_meta = {}

    model_ready = model_cfg is not None
    key_valid: Optional[bool] = None
    key_status_message = "尚未設定模型連線"
    if model_cfg is not None:
        if verify_live:
            probe = test_model_endpoint(
                base_url=model_cfg.base_url,
                api_key=model_cfg.glm_key,
                model=model_cfg.glm_model,
                timeout=3.0,
            )
            key_valid = bool(probe.get("ok"))
            key_status_message = str(probe.get("message") or ("金鑰有效" if key_valid else "金鑰驗證失敗"))
        else:
            key_status_message = "已載入設定（未進行線上即時驗證）"
    installed_cli_count = sum(1 for c in cli_items if c["installed"])
    needs_setup = (not model_ready) or (key_valid is False) or (installed_cli_count == 0)

    return {
        "ok": True,
        "needs_setup": needs_setup,
        "installed_cli_count": installed_cli_count,
        "clis": cli_items,
        "git": {"installed": bool(git_path), "path": git_path},
        "model": {
            "ready": model_ready,
            "key_valid": key_valid,
            "key_status_message": key_status_message,
            "file": cfg_path,
            "example_file": os.path.join(base_dir, "litellm.local.example.json"),
            "default_base_url": DEFAULT_LITELLM_URL,
            "base_url": model_cfg.base_url if model_cfg else str(raw_meta.get("base_url") or DEFAULT_LITELLM_URL),
            "glm_model": model_cfg.glm_model if model_cfg else str(raw_meta.get("glm_model") or "glm-5.3"),
            "d1_model": model_cfg.d1_model if model_cfg else str(raw_meta.get("d1_model") or "Decision-1"),
            "has_key": bool(model_cfg and model_cfg.glm_key),
            "key_preview": _mask_key(model_cfg.glm_key) if model_cfg else "",
            "has_d1_key": bool(model_cfg and model_cfg.decision1_key),
            "d1_key_preview": _mask_key(model_cfg.decision1_key) if model_cfg else "",
            "sovereign_base_url": model_cfg.sovereign_base_url if model_cfg else str(raw_meta.get("sovereign_base_url") or ""),
            "has_sovereign_key": bool((model_cfg and model_cfg.sovereign_key) or raw_meta.get("sovereign_key")),
            "sovereign_key_preview": _mask_key(model_cfg.sovereign_key if model_cfg else str(raw_meta.get("sovereign_key") or "")),
            "embedding_model": model_cfg.embedding_model if model_cfg else str(raw_meta.get("embedding_model") or "qwen3-embedding"),
            "embedding_dim": model_cfg.embedding_dim if model_cfg else int(raw_meta.get("embedding_dim") or 2560),
            "rerank_model": model_cfg.rerank_model if model_cfg else str(raw_meta.get("rerank_model") or "bge-reranker-v2-m3"),
            "wiki_base_url": str(raw_meta.get("wiki_base_url") or ""),
            "has_wiki_key": bool(raw_meta.get("wiki_key")),
            "wiki_key_preview": _mask_key(str(raw_meta.get("wiki_key") or "")),
        },
    }


def _normalize_base_url(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def test_model_endpoint(base_url: str, api_key: str = "", model: str = "", timeout: float = 6.0) -> Dict[str, Any]:
    """對使用者輸入的 Base URL（IP:Port 或網域）發送輕量 /models 連線測試。"""
    url = _normalize_base_url(base_url)
    parsed = urlparse(url)
    if not url or parsed.scheme not in ("http", "https") or not parsed.netloc:
        return {"ok": False, "status_code": 0, "latency_ms": 0, "models": [], "message": "請輸入有效的網址或 IP:Port（例如 http://127.0.0.1:11434/v1）"}

    candidates = [url + "/models"] if url.endswith("/v1") else [url + "/v1/models", url + "/models"]
    headers: Dict[str, str] = {"Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    last_err = ""
    t0 = time.monotonic()
    for target in candidates:
        try:
            resp = requests.get(target, headers=headers, timeout=timeout)
            latency_ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                models: List[str] = []
                try:
                    data = resp.json()
                    items = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for item in items[:50]:
                        if isinstance(item, dict) and item.get("id"):
                            models.append(str(item["id"]))
                        elif isinstance(item, str):
                            models.append(item)
                except ValueError:
                    pass
                note = f"連線成功（{latency_ms}ms，偵測到 {len(models)} 個模型）" if models else f"連線成功（{latency_ms}ms）"
                if model and models and model not in models:
                    note += f"；注意：清單中未見「{model}」"
                return {
                    "ok": True,
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                    "models": models,
                    "message": note,
                }
            if resp.status_code in (401, 403):
                return {
                    "ok": False,
                    "status_code": resp.status_code,
                    "latency_ms": latency_ms,
                    "models": [],
                    "message": f"伺服器可連線，但 API Key 驗證失敗（HTTP {resp.status_code}）",
                }
            last_err = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)

    latency_ms = int((time.monotonic() - t0) * 1000)
    return {
        "ok": False,
        "status_code": 0,
        "latency_ms": latency_ms,
        "models": [],
        "message": f"無法連線至 {url}：{last_err[:180]}",
    }


def save_model_config(base_dir: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """將使用者在精靈中輸入的 IP/Port、模型與金鑰安全寫入 litellm.local.json。"""
    cfg_path = _litellm_path(base_dir)
    existing: Dict[str, Any] = {}
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, ValueError):
            existing = {}

    base_url = _normalize_base_url(str(payload.get("base_url") or existing.get("base_url") or ""))
    if not base_url:
        raise ValueError("缺少 base_url（例如 http://127.0.0.1:11434/v1）")

    key_in = str(payload.get("key") or payload.get("glm_key") or "").strip()
    final_key = key_in or str(existing.get("key") or existing.get("glm_key") or "").strip()
    if not final_key:
        # 本機 Ollama 等免金鑰端點允許填入佔位字串
        final_key = "local-no-key"

    d1_in = str(payload.get("decision1_key") or "").strip()
    final_d1 = d1_in or str(existing.get("decision1_key") or "").strip() or final_key

    glm_model = str(payload.get("glm_model") or existing.get("glm_model") or "glm-5.3").strip()
    d1_model = str(payload.get("d1_model") or existing.get("d1_model") or "Decision-1").strip()

    sov_url_in = payload.get("sovereign_base_url")
    sovereign_base_url = (
        _normalize_base_url(str(sov_url_in))
        if sov_url_in is not None and str(sov_url_in).strip()
        else ("" if sov_url_in == "" else str(existing.get("sovereign_base_url") or ""))
    )
    sov_key_in = str(payload.get("sovereign_key") or "").strip()
    final_sov_key = sov_key_in or str(existing.get("sovereign_key") or "").strip()
    embedding_model = str(payload.get("embedding_model") or existing.get("embedding_model") or "qwen3-embedding").strip()
    raw_dim = payload.get("embedding_dim") if payload.get("embedding_dim") not in (None, "") else existing.get("embedding_dim", 2560)
    try:
        embedding_dim = int(raw_dim)
    except (TypeError, ValueError):
        embedding_dim = 2560
    rerank_model = str(payload.get("rerank_model") or existing.get("rerank_model") or "bge-reranker-v2-m3").strip()

    wiki_url_in = payload.get("wiki_base_url")
    wiki_base_url = (
        _normalize_base_url(str(wiki_url_in))
        if wiki_url_in is not None and str(wiki_url_in).strip()
        else ("" if wiki_url_in == "" else str(existing.get("wiki_base_url") or ""))
    )
    wiki_key_in = str(payload.get("wiki_key") or "").strip()
    final_wiki_key = wiki_key_in or str(existing.get("wiki_key") or "").strip()

    merged = dict(existing)
    merged["base_url"] = base_url
    merged["key"] = final_key
    merged["decision1_key"] = final_d1
    merged["glm_model"] = glm_model
    merged["d1_model"] = d1_model
    merged["sovereign_base_url"] = sovereign_base_url
    merged["sovereign_key"] = final_sov_key
    merged["embedding_model"] = embedding_model
    merged["embedding_dim"] = embedding_dim
    merged["rerank_model"] = rerank_model
    merged["wiki_base_url"] = wiki_base_url
    merged["wiki_key"] = final_wiki_key

    tmp_path = cfg_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, cfg_path)

    return {
        "ok": True,
        "base_url": base_url,
        "glm_model": glm_model,
        "d1_model": d1_model,
        "key_preview": _mask_key(final_key),
        "d1_key_preview": _mask_key(final_d1),
        "sovereign_base_url": sovereign_base_url,
        "sovereign_key_preview": _mask_key(final_sov_key),
        "embedding_model": embedding_model,
        "embedding_dim": embedding_dim,
        "rerank_model": rerank_model,
        "wiki_base_url": wiki_base_url,
        "wiki_key_preview": _mask_key(final_wiki_key),
    }


def export_safe_profile(cfg: Any, base_dir: str) -> Dict[str, Any]:
    """匯出不含任何金鑰與 Token 的安全設定快照（可安全分享或跨機還原）。"""
    model_cfg = load_model_config(_litellm_path(base_dir))
    ssh_hosts: List[Dict[str, Any]] = []
    ssh_file = os.path.join(base_dir, "sessions", "team", "ssh_hosts.json")
    if os.path.isfile(ssh_file):
        try:
            with open(ssh_file, encoding="utf-8") as f:
                raw_hosts = json.load(f)
            if isinstance(raw_hosts, list):
                ssh_hosts = [h for h in raw_hosts if isinstance(h, dict)]
        except (OSError, ValueError):
            ssh_hosts = []

    return {
        "schema": "buttonauto-profile-v1",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "team": {
            "team_max_running": int(getattr(cfg, "team_max_running", 4)),
            "team_daily_token_limit": int(getattr(cfg, "team_daily_token_limit", 0)),
            "team_cost_weight": float(getattr(cfg, "team_cost_weight", 0.3)),
            "team_context_rollover": float(getattr(cfg, "team_context_rollover", 0.7)),
        },
        "model_defaults": {
            "base_url": model_cfg.base_url if model_cfg else "",
            "glm_model": model_cfg.glm_model if model_cfg else "glm-5.3",
            "d1_model": model_cfg.d1_model if model_cfg else "Decision-1",
        },
        "ssh_hosts": ssh_hosts,
    }


def import_safe_profile(cfg: Any, config_path: str, base_dir: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """匯入安全設定檔（絕不覆蓋金鑰；只更新團隊上限、預算護欄與 SSH 主機清單）。"""
    if not isinstance(data, dict):
        raise ValueError("匯入資料必須是 JSON 物件")

    team_part = data.get("team") if isinstance(data.get("team"), dict) else {}
    if "team_max_running" in team_part:
        v = int(team_part["team_max_running"])
        cfg.team_max_running = float(max(1, min(16, v)))
    if "team_daily_token_limit" in team_part:
        v = int(team_part["team_daily_token_limit"])
        cfg.team_daily_token_limit = max(0, min(100_000_000, v))
    if "team_cost_weight" in team_part:
        v = float(team_part["team_cost_weight"])
        cfg.team_cost_weight = max(0.0, min(1.0, v))
    if "team_context_rollover" in team_part:
        v = float(team_part["team_context_rollover"])
        cfg.team_context_rollover = max(0.3, min(0.95, v))
    cfg.save(config_path)

    ssh_imported = 0
    if isinstance(data.get("ssh_hosts"), list):
        ssh_dir = os.path.join(base_dir, "sessions", "team")
        os.makedirs(ssh_dir, exist_ok=True)
        clean_hosts = [h for h in data["ssh_hosts"] if isinstance(h, dict) and h.get("host")]
        with open(os.path.join(ssh_dir, "ssh_hosts.json"), "w", encoding="utf-8") as f:
            json.dump(clean_hosts, f, ensure_ascii=False, indent=2)
        ssh_imported = len(clean_hosts)

    return {
        "ok": True,
        "team_daily_token_limit": int(getattr(cfg, "team_daily_token_limit", 0)),
        "team_max_running": int(getattr(cfg, "team_max_running", 4)),
        "ssh_hosts_imported": ssh_imported,
    }
