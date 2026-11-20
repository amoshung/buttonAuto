"""不連網的假模型路由（測試與示範用）。

每次呼叫也產生 ModelCall（model "scripted"、token 0、標「示範」），讓示範房的時間軸與用量抽屜誠實顯示
「示範（假模型）」做了哪些判斷。
"""
import re
from typing import Any, Dict, List, Optional, Tuple

from team.models import PURPOSE_ALLOW, AllowVerdict, ModelConfig, ModelRouter, prob01
from team.usage import DEMO_MODEL

DEMO_LABEL = "示範（假模型）"  # 假模型的判斷一律標成示範，不冒用 Decision-1／glm 的名字
DANGER_RE = re.compile(r"\b(rm|del|Remove-Item|format|curl|wget)\b", re.I)


class ScriptedRouter(ModelRouter):
    """decide_allow 依危險指令關鍵字決定；probability 一律 0.9；write 回固定句子。"""

    def __init__(self, cfg: Optional[ModelConfig] = None):
        super().__init__(cfg, d1=self._no_network, glm=self._no_network)

    @staticmethod
    def _no_network(*_a, **_k):
        raise RuntimeError("ScriptedRouter 不連網")

    def _fake(self, kind: str, effort: str, ctx: Optional[Dict[str, Any]], purpose: str, result: str,
              detail: Optional[Dict[str, Any]] = None) -> None:
        with self._track(kind, DEMO_MODEL, effort, ctx, purpose) as t:
            t.detail = dict(detail or {})  # 時間軸展開看得到假模型被問了什麼、回了什麼
            t.start()
            t.got(None)
            t.tokens = "demo"
            t.result = result

    def decide_allow(self, state_text: str, ctx: Optional[Dict[str, Any]] = None) -> AllowVerdict:
        self._count("d1_calls")
        if DANGER_RE.search(state_text or ""):
            v = AllowVerdict(False, 0.5, 0.95, DEMO_LABEL, "（假模型）偵測到危險指令", "示範（假模型）allow 0.50 risk 0.95")
        else:
            v = AllowVerdict(True, 0.95, 0.05, DEMO_LABEL, "（假模型）判定安全", "示範（假模型）allow 0.95 risk 0.05")
        self._fake("d1", "-", ctx, PURPOSE_ALLOW, "allow %.2f risk %.2f" % (v.allow, v.risk),
                   {"question": "這個動作安全、可以自動允許嗎？（allow／risk）", "state_excerpt": state_text or "",
                    "answer": "allow %.2f、risk %.2f（假模型依危險指令關鍵字決定）" % (v.allow, v.risk)})
        return v

    def probability(self, state_text: str, question: str, ctx: Optional[Dict[str, Any]] = None) -> Optional[float]:
        self._count("d1_calls")
        lim = prob01((ctx or {}).get("pass_min"))
        res = "機率 0.90" if lim is None else ("0.90 ≥ %.2f 通過" % lim if 0.9 >= lim else "0.90 < %.2f 不通過" % lim)
        self._fake("d1", "-", ctx, (ctx or {}).get("purpose") or "是非題", res,
                   {"question": question, "state_excerpt": state_text or "", "answer": "q：0.90（假模型一律回 0.90）"})
        return 0.9

    def write(self, prompt: str, effort: str = "low", ctx: Optional[Dict[str, Any]] = None) -> str:
        self._count("glm_calls")
        idx = (prompt or "").find("目標：")
        goal = prompt[idx + len("目標："):].strip()[:30] if idx >= 0 else ""
        purpose = (ctx or {}).get("purpose") or "產生文字"
        if ctx and ctx.get("note"):
            text = "（示範假模型）%s：請對準目標「%s」，每條條件都用指令驗證。" % (purpose, goal)
        elif purpose == "L2 診斷問題根因" and any(k in (prompt or "") for k in ("FileNotFoundError", "reference", "外部資料", "git 外")):
            text = (
                '```json\n'
                '{"category":"env","evidence":["FileNotFoundError: 測試需要 git 外資料（如 reference/）"],'
                '"action":"測試依賴 git 以外的環境資料，建議標記為環境既有失敗或提供資料","unsure":[]}\n'
                '```'
            )
        else:
            text = "（假模型回覆）請繼續朝目標前進：" + goal
        self._fake("glm", "normal" if effort == "normal" else "low", ctx, purpose, "產生 %d 字" % len(text),
                   {"question": prompt or "", "answer": text})
        return text

    def second_opinion(self, question: str, material: str, ctx: Optional[Dict[str, Any]] = None) -> Tuple[Optional[bool], str]:
        """假審查模型：資料裡有危險指令關鍵字或「不可信」→ 不通過，其餘通過。"""
        self._count("glm_calls")
        bad = bool(DANGER_RE.search(material or "")) or "不可信" in (material or "")
        reason = "（假模型）偵測到危險或不可信的內容" if bad else "（假模型）看起來合理"
        self._fake("glm", "normal", ctx, (ctx or {}).get("purpose") or "代管：審查模型第二意見",
                   ("不通過：" if bad else "通過：") + reason,
                   {"question": question, "state_excerpt": material or "",
                    "answer": '{"ok": %s, "reason": "%s"}' % ("false" if bad else "true", reason)})
        return (not bad), reason

    def choose(self, state_text: str, question: str, options: List[Dict[str, str]],
               ctx: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Dict[str, float]]:
        """假 Decision-1：選第一個不是「我來處理」（pause）的選項。"""
        self._count("d1_calls")
        ids = [str(o.get("id")) for o in options or [] if o.get("id")]
        pick = next((i for i in ids if i != "pause"), ids[0] if ids else None)
        probs = {i: (0.9 if i == pick else 0.1) for i in ids}
        self._fake("d1", "-", ctx, (ctx or {}).get("purpose") or "代管：選擇選項",
                   "選「%s」（0.90）" % pick if pick else "沒有選項",
                   {"question": question, "state_excerpt": state_text or "",
                    "answer": "、".join("%s %.2f" % (k, v) for k, v in probs.items())})
        return pick, probs
