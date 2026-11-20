"""角色提示詞房間層級自動改寫（glm-5.3 一般思考量）與效果評估（Decision-1 比較前後回合，設計 §5.4.2、Task 12）。"""
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from team import personas, prompts, rounds
from team.models import call_ctx
from team.prompt_defaults import AUTO_ROLES, ROLE_LABELS
from team.schemas import AutotunePatch

log = logging.getLogger(__name__)

AUTOTUNE_MAX = 3
AUTOTUNE_WINDOW = 3
AUTOTUNE_MIN = 2


def summarize(round_list: List[Dict[str, Any]]) -> str:
    """把多輪 RoundStat 濃縮成一段統計摘要文字。"""
    if not round_list:
        return "尚無回合紀錄"
    stats = [rounds.RoundStat.from_dict(r) if isinstance(r, dict) else r for r in round_list]
    n = len(stats)
    first_c = len(stats[0].criteria_passed)
    last_c = len(stats[-1].criteria_passed)
    first_f = len(stats[0].new_failures)
    last_f = len(stats[-1].new_failures)
    mismatches = sum(1 for s in stats if s.evidence_mismatch)
    idle_count = 0
    for i, s in enumerate(stats):
        prev = stats[i - 1] if i > 0 else None
        if not rounds.progressed(prev, s):
            idle_count += 1
    return (
        "共 %d 輪；通過條件數 %d → %d；新增失敗數 %d → %d；無進展輪數 %d；證據不符 %d 次"
        % (n, first_c, last_c, first_f, last_f, idle_count, mismatches)
    )


def propose(
    router: Any,
    role: str,
    sections: Dict[str, str],
    summary: str,
    ctx: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, str]]:
    """請 glm-5.3（一般思考量）讀目前可編輯段与房間紀錄摘要（皆在 _Fence 內），輸出新的可編輯段；驗證不過回 None。"""
    if router is None:
        return None
    f = personas.Fence()
    cur_json = json.dumps(
        {k: str((sections or {}).get(k) or "") for k in prompts.SECTION_KEYS},
        ensure_ascii=False,
        indent=2,
    )
    prompt = "\n\n".join(
        [
            "請針對角色「%s（%s）」根據以下房間執行紀錄摘要，改進其可編輯提示詞段落（identity、principles、checklist），"
            "幫助代理避免重複犯錯或更快達成進展。" % (ROLE_LABELS.get(role, role), role),
            f.notice,
            f.wrap("目前可編輯段落", cur_json),
            f.wrap("房間執行紀錄摘要", summary or "（無）"),
            "限制：\n"
            "1. 只能輸出 identity、principles、checklist 三個字串欄位的 ```json 區塊。\n"
            "2. 不可包含回覆契約字樣（如 status、回覆契約、忽略以上），也不可使用未定義的 {佔位符}。\n"
            '回覆格式：\n```json\n{"identity":"...","principles":"...","checklist":"..."}\n```',
        ]
    )
    call_c = dict(ctx or {})
    try:
        if hasattr(router, "write_json"):
            patch = call_ctx(router.write_json, prompt, AutotunePatch, effort="normal", ctx=call_c)
            data = {
                "identity": patch.identity,
                "principles": patch.principles,
                "checklist": patch.checklist,
            }
        else:
            reply = call_ctx(router.write, prompt, effort="normal", ctx=call_c)
            data = personas.parse_json_block(str(reply or ""))
    except Exception as e:
        log.warning("角色提示詞自動改寫失敗（fail-closed）：%s", type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    candidate: Dict[str, str] = {}
    for k in prompts.SECTION_KEYS:
        val = data.get(k)
        if isinstance(val, str) and val.strip():
            candidate[k] = val.strip()
        elif isinstance((sections or {}).get(k), str):
            candidate[k] = sections[k]
    if not candidate:
        return None
    if prompts.validate(candidate):
        return None
    return candidate


def evaluate(
    router: Any,
    role: str,
    before: List[Dict[str, Any]],
    after: List[Dict[str, Any]],
    old: Dict[str, str],
    new: Dict[str, str],
    ctx: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], str, str]:
    """用 Decision-1（不可用時退回 glm）比較套用前後的回合表現；輪數不足 2 輪回 (None, '輪數不足', '')。"""
    if len(before or []) < AUTOTUNE_MIN or len(after or []) < AUTOTUNE_MIN:
        return None, "輪數不足（套用前後各需至少 %d 輪）" % AUTOTUNE_MIN, ""
    if router is None:
        return None, "模型路由器不可用", ""
    before_sum = summarize(list(before)[-AUTOTUNE_WINDOW:])
    after_sum = summarize(list(after)[:AUTOTUNE_WINDOW])
    state_material = (
        "角色：%s（%s）\n"
        "套用前表現（最近 %d 輪）：%s\n"
        "套用後表現（%d 輪）：%s\n"
        "舊版原則：%s\n"
        "新版原則：%s"
        % (
            ROLE_LABELS.get(role, role),
            role,
            len(list(before)[-AUTOTUNE_WINDOW:]),
            before_sum,
            len(list(after)[:AUTOTUNE_WINDOW]),
            after_sum,
            (old or {}).get("principles", "")[:600],
            (new or {}).get("principles", "")[:600],
        )
    )
    opts = [
        {"id": "keep", "label": "保留新版"},
        {"id": "revert", "label": "退回舊版"},
    ]
    call_c = dict(ctx or {})
    try:
        pick, probs = call_ctx(
            router.choose,
            state_material,
            "比較套用前後的回合表現，哪一版提示詞效果較好？",
            opts,
            ctx=call_c,
        )
    except Exception as e:
        return None, "評估呼叫失敗：%s" % type(e).__name__, ""
    if pick not in ("keep", "revert"):
        return None, "模型未能做出選擇", str(call_c.get("model_used") or "")
    model_used = str(call_c.get("model_used") or getattr(router, "d1_name", "Decision-1"))
    p_keep = (probs or {}).get("keep")
    p_rev = (probs or {}).get("revert")
    score_txt = (
        "（keep=%.2f, revert=%.2f）" % (p_keep, p_rev)
        if isinstance(p_keep, (int, float)) and isinstance(p_rev, (int, float))
        else ""
    )
    verdict_txt = "Decision-1 判定較好" if pick == "keep" else "效果未改善，退回上一版"
    reason = "%s（%s）%s：%s vs %s" % (verdict_txt, model_used, score_txt, before_sum, after_sum)
    return pick, reason, model_used
