"""AI 服務連線設定（Base URL、金鑰、模型）。

角色分工：
- 中鋼主權 AI 的視覺模型（vision.model，預設 gemma-4-31b）：詳細 OCR，判讀畫面狀態。
  實測 gemma-4-31b 抄指令最準也最快；qwen-vl 抄中文長句略好，但常漏指令裡的符號
- 判斷模型（judge_model，預設 gemma-4-31b，同一個服務）：快速決策與擬回覆
- 決策可改用 Decision-1（decision_backend = "decision1"），機率較準但要付費

設定檔是 secrets.local.json（已列入 .gitignore）。金鑰有兩種來源：
- key_source = "file"：從 key_file 指定的檔案讀取（檔案內容只放金鑰本身）
- key_source = "manual"：使用面板上手動輸入、存在 secrets.local.json 的 key
"""
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SECRETS_PATH = os.path.join(BASE_DIR, "secrets.local.json")


@dataclass
class EndpointSettings:
    base_url: str = ""
    model: str = ""
    key: str = ""
    key_file: str = ""

    def resolve_key(self, key_source: str, base_dir: str = BASE_DIR) -> str:
        if key_source == "file":
            if not self.key_file:
                return ""
            path = self.key_file if os.path.isabs(self.key_file) else os.path.join(base_dir, self.key_file)
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    return f.read().strip()
            except OSError:
                return ""
        return self.key.strip()


def _default_vision() -> EndpointSettings:
    # 中鋼主權 AI：model 是做詳細 OCR / 畫面判讀的視覺模型
    return EndpointSettings(model="gemma-4-31b", key_file="中鋼主權AI_KEY.md")


def _default_decision() -> EndpointSettings:
    return EndpointSettings(model="Decision-1", key_file="Y6P2_liteLLMAPI.md")


# 決策方式：llm = 用中鋼主權 AI 的判斷模型（免費）；decision1 = 用 Decision-1（LiteLLM，依用量計費）
DECISION_BACKENDS = ("llm", "decision1")


@dataclass
class AISettings:
    key_source: str = "file"
    decision_backend: str = "llm"
    # 中鋼主權 AI 上負責快速判斷與擬回覆的模型（與 vision 共用 Base URL 和金鑰）
    judge_model: str = "gemma-4-31b"
    vision: EndpointSettings = field(default_factory=_default_vision)
    decision: EndpointSettings = field(default_factory=_default_decision)

    @classmethod
    def load(cls, path: str = DEFAULT_SECRETS_PATH) -> "AISettings":
        s = cls()
        if not os.path.exists(path):
            return s
        try:
            with open(path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
        except Exception:
            return s
        if data.get("key_source") in ("file", "manual"):
            s.key_source = data["key_source"]
        if data.get("decision_backend") in DECISION_BACKENDS:
            s.decision_backend = data["decision_backend"]
        if isinstance(data.get("judge_model"), str) and data["judge_model"].strip():
            s.judge_model = data["judge_model"].strip()
        for name in ("vision", "decision"):
            section = data.get(name) or {}
            ep: EndpointSettings = getattr(s, name)
            for k in ("base_url", "model", "key", "key_file"):
                if isinstance(section.get(k), str):
                    setattr(ep, k, section[k].strip())
        return s

    def save(self, path: str = DEFAULT_SECRETS_PATH) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)

    def vision_key(self) -> str:
        return self.vision.resolve_key(self.key_source)

    def decision_key(self) -> str:
        return self.decision.resolve_key(self.key_source)
