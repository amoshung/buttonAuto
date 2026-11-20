"""升級階梯狀態機（L1→L2→L3→L4→stop）與選擇性預算檢查（設計 §5.1.2、§5.1.3）。"""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

LEVELS = ("L1", "L2", "L3", "L4", "stop")
DIAG_CATS = ("check", "code", "env", "system")
REVIEW_DIAG_CATS = ("review", "code", "unclear", "system")
IDLE_TO_ESCALATE = 2
BUDGET_WARN = 0.8
BUDGET_KEYS = ("seconds", "rounds", "tokens")

_NEXT_LEVEL = {"L1": "L2", "L3": "L4", "L4": "stop"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Ladder:
    level: str = "L1"
    idle: int = 0
    code_returns: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)
    tried: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Ladder":
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in (d or {}).items() if k in names}
        if data.get("level") not in LEVELS:
            data["level"] = "L1"
        return cls(**data)

    def record(self, progress: bool, reason: str = "") -> Optional[Tuple[str, str]]:
        """記錄一輪進展結果；若連續 IDLE_TO_ESCALATE 輪無進展則升級並回傳 (from, to)，否則回 None。"""
        if progress:
            self.idle = 0
            return None
        if self.level in ("L2", "stop"):
            return None
        self.idle += 1
        if self.idle >= IDLE_TO_ESCALATE:
            old = self.level
            nxt = _NEXT_LEVEL.get(old, "stop")
            self.level = nxt
            self.idle = 0
            self.history.append(
                {
                    "at": _now(),
                    "from": old,
                    "to": nxt,
                    "reason": reason or ("連續 %d 輪無進展" % IDLE_TO_ESCALATE),
                }
            )
            return old, nxt
        return None

    def after_diagnosis(self, category: str, reason: str = "") -> Tuple[str, str]:
        """L2 診斷完成後的轉移：code 可回 L1 一次，第二次去 L3；其他分類直接 stop。"""
        old = self.level
        if category == "code" and self.code_returns == 0:
            nxt = "L1"
            self.code_returns = 1
        elif category == "code":
            nxt = "L3"
        else:
            nxt = "stop"
        self.level = nxt
        self.idle = 0
        self.history.append(
            {
                "at": _now(),
                "from": old,
                "to": nxt,
                "reason": reason or ("診斷分類：%s" % category),
            }
        )
        return old, nxt

    def reset(self, reason: str) -> None:
        """使用者回答停下項目後回到 L1、無進展計數歸零。"""
        old = self.level
        self.level = "L1"
        self.idle = 0
        self.history.append({"at": _now(), "from": old, "to": "L1", "reason": reason})


def _valid_limit(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def budget_state(limits: Dict[str, Any], spent: Dict[str, Any]) -> Tuple[str, str]:
    """檢查預算狀態：回傳 ('ok'|'warn'|'out', 項目名)。預設無上限（limits 為空或值為 None/0）。"""
    lim_map = limits or {}
    sp_map = spent or {}
    for k in BUDGET_KEYS:
        lim = _valid_limit(lim_map.get(k))
        if lim is None:
            continue
        cur = float(sp_map.get(k) or 0.0)
        if cur >= lim:
            return "out", k
    for k in BUDGET_KEYS:
        lim = _valid_limit(lim_map.get(k))
        if lim is None:
            continue
        cur = float(sp_map.get(k) or 0.0)
        if cur >= BUDGET_WARN * lim:
            return "warn", k
    return "ok", ""


def grant(limits: Dict[str, Any], spent: Dict[str, Any] = None) -> Dict[str, Any]:
    """「再給一份預算」：每個有設上限的項目加上原始上限（記在 limits['base']）。"""
    out = dict(limits or {})
    base = dict(out.get("base") or {})
    for k in BUDGET_KEYS:
        lim = _valid_limit(out.get(k))
        if lim is None:
            continue
        b = _valid_limit(base.get(k)) or lim
        base[k] = int(b) if k in ("rounds", "tokens") else b
        new_val = lim + b
        out[k] = int(new_val) if k in ("rounds", "tokens") else new_val
    if base:
        out["base"] = base
    return out
