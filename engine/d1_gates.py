"""Decision-1 128 題滿載打包閘門（Single-Call Multi-Question Packing）。

核心原則：
1. Decision-1 只計 input tokens（output 免費），每次往返約 1.5 秒、單次支援 1～128 題。
2. 永遠由 Python / LangGraph 節點直接呼叫，絕不讓大語言模型當搬運工。
3. 把同一份 state 的所有相關題目打包成 1 次 /v1/systemone 呼叫（自動切批確保每批 ≤ 128 題）。
4. Decision-1 不可用時一律 fail-safe 降級（不阻斷流程、審查保守走大模型）。
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from engine.schemas import (
    D1FailureDiagnosis,
    D1GoalAuditResult,
    D1PlanCheckResult,
    D1ReviewGateResult,
    D1TriageItem,
    PlanSpec,
    UnitSpec,
    VerifyReport,
)

log = logging.getLogger(__name__)

MAX_QUESTIONS_PER_CALL = 128
DEFAULT_CONCURRENCY = 8

# Decision-1 審查分流預設門檻（可依專案嚴格度調整）
FAST_PASS_THRESHOLDS = {
    "aligned_with_unit": 0.80,
    "real_assertions": 0.80,
    "no_scope_creep": 0.75,
    "no_stub_or_todo": 0.80,
    "security_risk_max": 0.25,
    "concurrency_risk_max": 0.30,
    "breaking_change_max": 0.30,
    "needs_deep_review_max": 0.40,
}

# 審查維度中文焦點說明（當需升級給大模型 Micro-Review 時，只聚焦低分維度）
REVIEW_FOCUS_LABELS = {
    "aligned_with_unit": "實作與本單元目標對齊度不足",
    "no_scope_creep": "存在超出本單元範圍的改動",
    "no_unrelated_edits": "包含與本單元無關的檔案修改",
    "real_assertions": "測試斷言強度不足或疑似假測試",
    "no_skipped_tests": "測試中存在跳過或過度 mock 核心邏輯",
    "covers_edge_cases": "缺少邊界條件或異常路徑覆蓋",
    "error_handling_complete": "錯誤處理與資源釋放不完整",
    "no_stub_or_todo": "殘留未實作的 TODO / stub / placeholder",
    "security_risk": "潛在安全性風險（注入、路徑穿越、憑證外洩等）",
    "concurrency_risk": "潛在並行／狀態競爭／死鎖風險",
    "breaking_change": "可能造成既有公開介面或資料格式相容性破壞",
}


def _prob(answers: Dict[str, Any], key: str, default: float = 0.5) -> float:
    item = answers.get(key)
    if not isinstance(item, dict):
        return default
    raw = item.get("noul", item.get("probability", default))
    try:
        val = float(raw)
        if math.isnan(val) or math.isinf(val):
            return default
        return max(0.0, min(1.0, val))
    except (TypeError, ValueError):
        return default


def _choice(answers: Dict[str, Any], key: str, allowed: Sequence[str], default: str) -> str:
    item = answers.get(key)
    if not isinstance(item, dict):
        return default
    val = str(item.get("choice") or "").strip()
    return val if val in allowed else default


class D1Packer:
    """Decision-1 多題打包呼叫器。

    支援注入：
    - `d1_fn(state: str, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]`（測試／直接客戶端）
    - 或既有 `router`（`team.models.ModelRouter`），透過其 `_d1` / `cfg` 連線。
    """

    def __init__(
        self,
        d1_fn: Optional[Callable[[str, Dict[str, Dict[str, Any]]], Dict[str, Any]]] = None,
        router: Any = None,
        max_workers: int = DEFAULT_CONCURRENCY,
        on_trace: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._d1_fn = d1_fn
        self._router = router
        self.max_workers = max(1, int(max_workers))
        self._on_trace = on_trace

    def _trace(self, gate: str, **fields: Any) -> None:
        if self._on_trace is not None:
            try:
                self._on_trace("d1_gate", gate=gate, **fields)
            except Exception:
                pass

    @property
    def available(self) -> bool:
        if self._d1_fn is not None:
            return True
        if self._router is not None:
            cfg = getattr(self._router, "cfg", None)
            if cfg is not None and getattr(cfg, "base_url", "") and getattr(cfg, "decision1_key", ""):
                return True
            if callable(getattr(self._router, "_d1", None)):
                return True
        return False

    def _call_raw(
        self,
        state: str,
        questions: Dict[str, Dict[str, Any]],
        *,
        gate: str = "raw",
    ) -> Optional[Dict[str, Any]]:
        """單次呼叫 Decision-1（自動確保題數 ≤ 128；超過時分批呼叫並合併 answers）。"""
        if not questions:
            return {}
        t0 = time.monotonic()
        items = list(questions.items())
        merged: Dict[str, Any] = {}
        batches = 0
        for i in range(0, len(items), MAX_QUESTIONS_PER_CALL):
            chunk = dict(items[i : i + MAX_QUESTIONS_PER_CALL])
            batches += 1
            ans = self._call_single(state, chunk)
            if ans is None:
                self._trace(
                    gate,
                    ok=False,
                    questions=len(items),
                    batches=batches,
                    elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
                )
                return None
            merged.update(ans)
        self._trace(
            gate,
            ok=True,
            questions=len(items),
            batches=batches,
            state_chars=len(state),
            elapsed_ms=round((time.monotonic() - t0) * 1000, 1),
        )
        return merged

    def _call_single(self, state: str, questions: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        try:
            if self._d1_fn is not None:
                res = self._d1_fn(state, questions)
                return res if isinstance(res, dict) else None
            if self._router is not None:
                d1_callable = getattr(self._router, "_d1", None)
                if callable(d1_callable):
                    res = d1_callable(state, questions)
                    if hasattr(res, "value"):
                        res = res.value
                    if isinstance(res, tuple) and res:
                        res = res[0]
                    return res if isinstance(res, dict) else None
        except Exception as e:
            log.info("Decision-1 打包呼叫失敗（將安全降級）：%s", type(e).__name__)
        return None

    # ------------------------------------------------------------------
    # [D1 #1] Plan 全量體檢包（單次最多 40 單元 × 3 題 + 全局 3 題 = 123 題）
    # ------------------------------------------------------------------
    def check_plan_quality(self, plan: PlanSpec) -> D1PlanCheckResult:
        if not plan.units:
            return D1PlanCheckResult(ok=False, covers_full_goal=0.0, used_d1=False)
        if not self.available:
            return D1PlanCheckResult(ok=True, used_d1=False)

        unit_lines = [
            f"- [{u.id}] {u.title} | 驗證指令: {u.check_cmd or '(沿用全域測試)'} | 範圍: {', '.join(u.scope_files) or '(自動)'}"
            for u in plan.units
        ]
        state = (
            f"【專案任務目標】\n{plan.objective}\n\n"
            f"【全域測試指令】\n{plan.global_test_cmd or '(未指定)'}\n\n"
            f"【拆解出的實作單元（共 {len(plan.units)} 項）】\n" + "\n".join(unit_lines)
        )

        # 全局 3 題 + 每單元 3 題（自動依 128 題上限分批）
        questions: Dict[str, Dict[str, Any]] = {
            "covers_full_goal": {
                "type": "noul",
                "instructions": "上述所有實作單元合起來，是否完整覆蓋專案任務目標的核心需求？",
            },
            "logical_order_valid": {
                "type": "noul",
                "instructions": "這些單元的排列順序是否符合合理依賴順序（基礎先做、整合後做）？",
            },
            "missing_critical_step": {
                "type": "noul",
                "instructions": "這份計畫是否遺漏了明顯不可或缺的關鍵實作或驗證步驟？",
            },
        }
        for idx, u in enumerate(plan.units):
            key_prefix = f"u{idx:02d}"
            questions[f"{key_prefix}_atomic"] = {
                "type": "noul",
                "instructions": f"單元 {u.id}（{u.title}）的粒度是否夠小、適合單一獨立對話回合內完成？",
            }
            questions[f"{key_prefix}_verifiable"] = {
                "type": "noul",
                "instructions": f"單元 {u.id}（{u.title}）是否具備客觀可執行的驗證方式（不靠主觀猜測）？",
            }
            questions[f"{key_prefix}_dep_ok"] = {
                "type": "noul",
                "instructions": f"單元 {u.id} 排在第 {idx + 1} 順位執行是否合理（不依賴尚未完成的後續單元）？",
            }

        answers = self._call_raw(state, questions, gate="plan_quality")
        if answers is None:
            return D1PlanCheckResult(ok=True, used_d1=False)

        covers = _prob(answers, "covers_full_goal", 0.8)
        order_ok = _prob(answers, "logical_order_valid", 0.8)
        missing = _prob(answers, "missing_critical_step", 0.2)
        weak_ids: List[str] = []
        unit_scores: Dict[str, Dict[str, float]] = {}

        for idx, u in enumerate(plan.units):
            kp = f"u{idx:02d}"
            sc = {
                "atomic": _prob(answers, f"{kp}_atomic", 0.8),
                "verifiable": _prob(answers, f"{kp}_verifiable", 0.8),
                "dep_ok": _prob(answers, f"{kp}_dep_ok", 0.8),
            }
            unit_scores[u.id] = sc
            if sc["atomic"] < 0.45 or sc["verifiable"] < 0.45 or sc["dep_ok"] < 0.40:
                weak_ids.append(u.id)

        ok = covers >= 0.55 and order_ok >= 0.55 and missing < 0.70 and len(weak_ids) <= max(1, len(plan.units) // 3)
        return D1PlanCheckResult(
            ok=ok,
            covers_full_goal=covers,
            logical_order_valid=order_ok,
            missing_critical_step=missing,
            weak_unit_ids=weak_ids,
            unit_scores=unit_scores,
            used_d1=True,
        )

    # ------------------------------------------------------------------
    # [D1 #2 + #4] 失敗歸因＋防死鎖合併診斷包（1 次呼叫問 8 題）
    # ------------------------------------------------------------------
    def classify_failure_and_circuit(
        self,
        unit: UnitSpec,
        report: VerifyReport,
        prev_failure_tail: str = "",
        attempts: int = 1,
        max_attempts: int = 3,
    ) -> D1FailureDiagnosis:
        if not self.available:
            action = "ask_human" if attempts >= max_attempts else "retry_same_strategy"
            return D1FailureDiagnosis(
                failure_class="code_bug",
                suggested_action=action,
                used_d1=False,
            )

        state = (
            f"【目前執行單元】{unit.id}: {unit.title}\n"
            f"【驗證指令】{report.cmd}（結束碼 {report.exit_code}，重試第 {attempts}/{max_attempts} 次）\n\n"
            f"【本次錯誤輸出尾段】\n{report.tail_output[-3500:]}\n\n"
            f"【前一次錯誤輸出尾段】\n{(prev_failure_tail or '（無，首次失敗）')[-1500:]}"
        )
        questions: Dict[str, Dict[str, Any]] = {
            "failure_class": {
                "type": "choice",
                "instructions": "這次驗證失敗最主要屬於哪一類原因？",
                "criteria": {
                    "code_bug": "程式碼邏輯錯誤、編譯錯誤、測試斷言失敗或缺少實作",
                    "env_issue": "外部環境限制（缺外部服務/資料庫、埠號占用、網路權限、外部唯讀目錄）",
                    "flaky_test": "偶發性逾時、時序抖動或暫時性鎖定，重跑可能通過",
                    "spec_mismatch": "驗收條件／檢查腳本本身定義矛盾、過時或與目標衝突",
                },
            },
            "same_root_cause": {
                "type": "noul",
                "instructions": "本次失敗與前一次失敗是否完全卡在同一個根因、毫無進展（原地打轉）？",
            },
            "is_external_env": {
                "type": "noul",
                "instructions": "此失敗是否完全由外部環境限制造成、代理在工作區內根本無法靠改程式修復？",
            },
            "is_flaky": {
                "type": "noul",
                "instructions": "此失敗是否屬於偶發性環境抖動（flaky），不改程式直接重跑一次就可能通過？",
            },
            "is_spec_broken": {
                "type": "noul",
                "instructions": "失敗原因是檢查指令本身寫錯或規格有誤，而非實作程式碼錯誤？",
            },
            "needs_human": {
                "type": "noul",
                "instructions": "目前狀況是否需要立刻暫停並交給人類決定（而非繼續自動重試）？",
            },
            "worth_changing_strategy": {
                "type": "noul",
                "instructions": "是否應該要求代理放棄原修法、改採完全不同的實作策略再試一次？",
            },
            "suggested_action": {
                "type": "choice",
                "instructions": "綜合以上判斷，系統下一步應該採取哪種處置？",
                "criteria": {
                    "retry_same_strategy": "明確程式錯誤，退回研發工程師依錯誤輸出修復",
                    "change_strategy": "連敗或原地打轉，要求研發工程師更換實作策略",
                    "waive_env": "純外部環境限制無法本機修復，紀錄審計軌跡並放行",
                    "ask_human": "規格衝突或反覆失敗，交給人類在收件匣裁決",
                },
            },
        }

        answers = self._call_raw(state, questions, gate="failure_circuit")
        if answers is None:
            action = "ask_human" if attempts >= max_attempts else "retry_same_strategy"
            return D1FailureDiagnosis(failure_class="code_bug", suggested_action=action, used_d1=False)

        fclass = _choice(answers, "failure_class", ("code_bug", "env_issue", "flaky_test", "spec_mismatch"), "code_bug")
        same_rc = _prob(answers, "same_root_cause", 0.0)
        ext_env = _prob(answers, "is_external_env", 0.0)
        needs_human = _prob(answers, "needs_human", 0.0)
        action = _choice(
            answers,
            "suggested_action",
            ("retry_same_strategy", "change_strategy", "waive_env", "ask_human"),
            "retry_same_strategy",
        )

        # 安全守衛：只有高信心外部環境才自動豁免；達上限強制交人類
        if action == "waive_env" and ext_env < 0.78:
            action = "ask_human" if attempts >= max_attempts else "change_strategy"
        if attempts >= max_attempts and action in ("retry_same_strategy", "change_strategy"):
            action = "ask_human"

        return D1FailureDiagnosis(
            failure_class=fclass,  # type: ignore[arg-type]
            same_root_cause=same_rc,
            is_external_env=ext_env,
            needs_human=needs_human,
            suggested_action=action,  # type: ignore[arg-type]
            used_d1=True,
        )

    # ------------------------------------------------------------------
    # [D1 #3] 單元全方位審查分流包（1 次呼叫問 15 題）
    # ------------------------------------------------------------------
    def evaluate_unit_review_gate(
        self,
        unit: UnitSpec,
        goal: str,
        diff_stat: str,
        diff_text: str,
        test_tail: str,
        fast_review_enabled: bool = True,
    ) -> D1ReviewGateResult:
        if not self.available:
            return D1ReviewGateResult(fast_pass=False, risk_level="unknown", used_d1=False)

        state = (
            f"【專案總目標】{goal}\n"
            f"【本單元目標】{unit.id}: {unit.title}\n"
            f"{unit.description}\n\n"
            f"【變更統計】\n{diff_stat[:2000]}\n\n"
            f"【驗證通過輸出】\n{test_tail[-2000:]}\n\n"
            f"【本單元 Diff】\n{diff_text[:18000]}"
        )
        questions: Dict[str, Dict[str, Any]] = {
            "aligned_with_unit": {"type": "noul", "instructions": "程式碼改動是否精準完成本單元目標？"},
            "no_scope_creep": {"type": "noul", "instructions": "改動是否嚴格限制在本單元範圍內，沒有順手亂改無關模組？"},
            "no_unrelated_edits": {"type": "noul", "instructions": "所有變更的檔案是否都與本單元直接相關？"},
            "real_assertions": {"type": "noul", "instructions": "測試是否真的驗證了本次改動的核心行為（而非永遠成立的假斷言）？"},
            "no_skipped_tests": {"type": "noul", "instructions": "測試是否沒有被 skip、註解掉或過度 mock 掉受測邏輯？"},
            "covers_edge_cases": {"type": "noul", "instructions": "改動與測試是否涵蓋基本異常或邊界情況？"},
            "error_handling_complete": {"type": "noul", "instructions": "新增程式碼的錯誤處理與資源清理是否合理完整？"},
            "no_stub_or_todo": {"type": "noul", "instructions": "程式碼中是否沒有殘留未實作的 stub、pass、todo 或佔位符？"},
            "security_risk": {"type": "noul", "instructions": "這段改動是否帶有安全性風險（命令注入、SQL 注入、路徑穿越、金鑰硬編碼）？"},
            "concurrency_risk": {"type": "noul", "instructions": "這段改動是否涉及多執行緒／非同步／共用狀態的競爭或死鎖風險？"},
            "breaking_change": {"type": "noul", "instructions": "這段改動是否可能破壞既有公開 API、設定檔格式或下游相容性？"},
            "large_or_complex_diff": {"type": "noul", "instructions": "這段 diff 的邏輯複雜度是否高到需要資深審查者逐行檢視？"},
            "code_cleanliness": {"type": "noul", "instructions": "程式碼結構是否簡潔清晰、沒有明顯重複或死碼？"},
            "risk_level": {
                "type": "choice",
                "instructions": "綜合評估本次單元改動的整體風險等級：",
                "criteria": {
                    "low": "小範圍、邏輯單純、測試扎實、無安全或相容性風險",
                    "medium": "中等改動或牽涉多個檔案，建議抽查關鍵路徑",
                    "high": "核心邏輯、安全敏感、並行狀態、介面變更或存在明顯疑慮",
                },
            },
            "needs_deep_review": {
                "type": "noul",
                "instructions": "本次單元改動是否需要額外喚起大型語言模型做逐行深度程式碼審查？",
            },
        }

        answers = self._call_raw(state, questions, gate="review_gate")
        if answers is None:
            return D1ReviewGateResult(fast_pass=False, risk_level="unknown", used_d1=False)

        scores = {k: _prob(answers, k, 0.5) for k in questions if k != "risk_level"}
        risk = _choice(answers, "risk_level", ("low", "medium", "high"), "medium")

        # 找出低分／高風險維度，供大模型 Micro-Review 精準聚焦
        focus: List[str] = []
        positive_keys = (
            "aligned_with_unit", "no_scope_creep", "no_unrelated_edits",
            "real_assertions", "no_skipped_tests", "covers_edge_cases",
            "error_handling_complete", "no_stub_or_todo",
        )
        negative_keys = ("security_risk", "concurrency_risk", "breaking_change")
        for k in positive_keys:
            if scores[k] < 0.70 and k in REVIEW_FOCUS_LABELS:
                focus.append(f"{REVIEW_FOCUS_LABELS[k]}（D1={scores[k]:.2f}）")
        for k in negative_keys:
            if scores[k] >= 0.35 and k in REVIEW_FOCUS_LABELS:
                focus.append(f"{REVIEW_FOCUS_LABELS[k]}（D1={scores[k]:.2f}）")

        th = FAST_PASS_THRESHOLDS
        fast_pass = (
            fast_review_enabled
            and risk == "low"
            and scores["aligned_with_unit"] >= th["aligned_with_unit"]
            and scores["real_assertions"] >= th["real_assertions"]
            and scores["no_scope_creep"] >= th["no_scope_creep"]
            and scores["no_stub_or_todo"] >= th["no_stub_or_todo"]
            and scores["security_risk"] < th["security_risk_max"]
            and scores["concurrency_risk"] < th["concurrency_risk_max"]
            and scores["breaking_change"] < th["breaking_change_max"]
            and scores["needs_deep_review"] < th["needs_deep_review_max"]
        )
        return D1ReviewGateResult(
            fast_pass=fast_pass,
            risk_level=risk,  # type: ignore[arg-type]
            scores=scores,
            focus_areas=focus,
            used_d1=True,
        )

    # ------------------------------------------------------------------
    # [D1 #5] 結案前全盤覆檢閘門（單次 4 題，判定是否還有 Gap 需開下一輪）
    # ------------------------------------------------------------------
    def evaluate_goal_completion(
        self,
        goal: str,
        units: Sequence[UnitSpec],
        artifacts: Dict[str, Any],
        round_num: int = 1,
    ) -> D1GoalAuditResult:
        if not self.available or not goal.strip():
            return D1GoalAuditResult(needs_followup_round=False, used_d1=False)

        unit_summary = "\n".join(
            f"- [{u.id}] ({u.status}) {u.title}"
            + (f" | commit={artifacts[u.id].get('commit_sha', '')[:7]}" if u.id in artifacts else "")
            for u in units
        )
        state = (
            f"【房間總目標（第 {round_num} 輪覆檢）】\n{goal}\n\n"
            f"【已完成單元清單（共 {len(units)} 項）】\n{unit_summary}"
        )
        questions: Dict[str, Dict[str, Any]] = {
            "goal_fully_satisfied": {
                "type": "noul",
                "instructions": "從已完成單元的覆蓋範圍來看，房間總目標的核心需求是否已全部達成？",
            },
            "remaining_gaps_exist": {
                "type": "noul",
                "instructions": "是否仍有明顯未實作、只停留在盤點文件而未實際修復、或目標明確要求但尚未執行的 Gap？",
            },
            "all_units_verified_or_waived": {
                "type": "noul",
                "instructions": "各單元的結案狀態是否合理，沒有草率帶過核心功能？",
            },
            "next_step": {
                "type": "choice",
                "instructions": "目前應直接送出合併確認，還是再規劃一輪補充單元解決剩餘 Gap？",
                "criteria": {
                    "merge_ready": "目標已完整滿足，可送交人工合併確認",
                    "followup_round": "仍有明顯 Gap 或目標要求的後續階段未完成，應生成下一輪補充單元",
                },
            },
        }
        answers = self._call_raw(state, questions, gate="goal_audit")
        if answers is None:
            return D1GoalAuditResult(needs_followup_round=False, used_d1=False)

        sat = _prob(answers, "goal_fully_satisfied", 0.8)
        gaps = _prob(answers, "remaining_gaps_exist", 0.2)
        nxt = _choice(answers, "next_step", ("merge_ready", "followup_round"), "merge_ready")
        need_more = (nxt == "followup_round") or (gaps >= 0.68 and sat < 0.65)
        return D1GoalAuditResult(
            goal_fully_satisfied=sat,
            remaining_gaps_exist=gaps,
            needs_followup_round=need_more,
            used_d1=True,
        )

    # ------------------------------------------------------------------
    # [D1 選配] 通用多項目批次分診包（單次打包 15~25 項 × 4 題 ≤ 125 題，多批並發）
    # ------------------------------------------------------------------
    def batch_triage_items(
        self,
        items: Sequence[Dict[str, str]],
        goal: str,
        *,
        batch_size: int = 20,
        max_state_chars: int = 28000,
    ) -> List[D1TriageItem]:
        """通用批次分診：每項含 `id`、`label`、`content`，在 128 題上限內自動打包並發掃描。"""
        if not items:
            return []
        if not self.available:
            return [
                D1TriageItem(item_id=str(it.get("id") or i), label=str(it.get("label") or ""), needs_work=True)
                for i, it in enumerate(items)
            ]

        # 依題數（每項 4 題，上限 30 項 = 120 題 ≤ 128）與字數上限切批
        safe_batch = max(1, min(batch_size, MAX_QUESTIONS_PER_CALL // 4))
        batches: List[List[Dict[str, str]]] = []
        cur: List[Dict[str, str]] = []
        cur_chars = 0
        for it in items:
            piece_len = len(str(it.get("content") or "")) + len(str(it.get("label") or "")) + 80
            if cur and (len(cur) >= safe_batch or cur_chars + piece_len > max_state_chars):
                batches.append(cur)
                cur = []
                cur_chars = 0
            cur.append(dict(it))
            cur_chars += piece_len
        if cur:
            batches.append(cur)

        def _run_batch(batch: List[Dict[str, str]]) -> List[D1TriageItem]:
            blocks = []
            questions: Dict[str, Dict[str, Any]] = {}
            for idx, it in enumerate(batch):
                tag = f"U{idx:02d}"
                lbl = str(it.get("label") or it.get("id") or tag)
                content = str(it.get("content") or "")[: max(400, max_state_chars // max(1, len(batch)))]
                blocks.append(f"=== {tag}: {lbl} ===\n{content}")
                questions[f"{tag}_needs_work"] = {
                    "type": "noul",
                    "instructions": f"針對目標「{goal[:160]}」，項目 {tag}（{lbl}）是否仍存在缺口、缺陷或待處理工作？",
                }
                questions[f"{tag}_has_stub"] = {
                    "type": "noul",
                    "instructions": f"項目 {tag}（{lbl}）中是否明顯殘留未完成標記、stub 或不完整邏輯？",
                }
                questions[f"{tag}_high_priority"] = {
                    "type": "noul",
                    "instructions": f"項目 {tag}（{lbl}）是否屬於高優先度／核心路徑，應優先安排修改？",
                }
                questions[f"{tag}_category"] = {
                    "type": "choice",
                    "instructions": f"項目 {tag}（{lbl}）目前狀態歸類：",
                    "criteria": {
                        "done": "已完成且符合目標，無需修改",
                        "minor": "小幅缺口或待補齊細節",
                        "major": "重大功能缺口、邏輯錯誤或待重構",
                        "missing": "尚未實作或完全缺失",
                    },
                }
            state = f"【盤點目標】{goal}\n\n" + "\n\n".join(blocks)
            answers = self._call_raw(state, questions, gate="batch_triage")
            out: List[D1TriageItem] = []
            for idx, it in enumerate(batch):
                tag = f"U{idx:02d}"
                iid = str(it.get("id") or tag)
                lbl = str(it.get("label") or iid)
                if answers is None:
                    out.append(D1TriageItem(item_id=iid, label=lbl, needs_work=True))
                    continue
                nw = _prob(answers, f"{tag}_needs_work", 0.5)
                stub = _prob(answers, f"{tag}_has_stub", 0.0)
                hp = _prob(answers, f"{tag}_high_priority", 0.5)
                cat = _choice(answers, f"{tag}_category", ("done", "minor", "major", "missing"), "minor")
                out.append(
                    D1TriageItem(
                        item_id=iid,
                        label=lbl,
                        needs_work=(cat != "done" or nw >= 0.45 or stub >= 0.50),
                        priority_score=round(0.5 * nw + 0.3 * hp + 0.2 * stub, 3),
                        category=cat,
                        scores={"needs_work": nw, "has_stub": stub, "high_priority": hp},
                    )
                )
            return out

        results: List[D1TriageItem] = []
        if len(batches) == 1:
            results.extend(_run_batch(batches[0]))
        else:
            with ThreadPoolExecutor(max_workers=min(self.max_workers, len(batches))) as pool:
                futs = [pool.submit(_run_batch, b) for b in batches]
                for fut in as_completed(futs):
                    results.extend(fut.result())
        results.sort(key=lambda x: (-int(x.needs_work), -x.priority_score, x.item_id))
        return results
