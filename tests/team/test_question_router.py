"""U3 單元測試：問題自動分流（選項類 -> Decision-1；描述類 -> GLM-5.3 + 唯讀參考來源）。"""
from team.fakes.fake_models import ScriptedRouter
from team.question_router import (
    MAX_QUESTION_STREAK,
    classify_question,
    extract_inline_options,
    is_destructive_question,
    route_question,
)

GOAL = "在 service-b 實作付款驗證並參考 shared_C 的介面"


def test_classify_multiple_choice_vs_descriptive():
    opts = [{"id": "opt_a", "label": "使用同步呼叫"}, {"id": "opt_b", "label": "使用非同步佇列"}]
    kind, resolved = classify_question("架構選擇", "請選擇實作方式", opts)
    assert kind == "multiple_choice"
    assert len(resolved) == 2

    inline = "請問這個錯誤碼應該選哪個？\nA) 回傳 400 Bad Request\nB) 回傳 422 Unprocessable Entity"
    assert len(extract_inline_options(inline)) == 2
    kind2, resolved2 = classify_question("錯誤碼選擇", inline, [])
    assert kind2 == "multiple_choice"
    assert [o["id"] for o in resolved2] == ["A", "B"]

    desc = "shared_C 裡面的 PaymentToken 欄位格式與簽章演算法是什麼？請說明。"
    kind3, resolved3 = classify_question("需要補充規格", desc, [])
    assert kind3 == "descriptive"
    assert resolved3 == []


def test_multiple_choice_routes_to_decision1():
    called = {"choose": 0, "write": 0}

    class SpyRouter(ScriptedRouter):
        def choose(self, state_text, question, options, ctx=None):
            called["choose"] += 1
            return "opt_a", {"opt_a": 0.88, "opt_b": 0.12}

        def write(self, prompt, effort="low", ctx=None):
            called["write"] += 1
            return "不應該呼叫 write"

    router = SpyRouter()
    opts = [{"id": "opt_a", "label": "使用同步呼叫"}, {"id": "opt_b", "label": "使用非同步佇列"}]
    res = route_question(router, goal=GOAL, title="架構選擇", detail="該用哪種方式？", options=opts)
    assert res["handoff"] is False
    assert res["kind"] == "multiple_choice"
    assert res["by"] == "Decision-1"
    assert res["chosen_id"] == "opt_a"
    assert "使用同步呼叫" in res["answer_text"]
    assert called["choose"] == 1
    assert called["write"] == 0


def test_descriptive_routes_to_glm53_with_readonly_reference_context(tmp_path):
    parent_a = tmp_path / "parent_A"
    sub_b = parent_a / "service_B"
    sub_c = parent_a / "shared_C"
    sub_b.mkdir(parents=True)
    sub_c.mkdir(parents=True)
    (sub_c / "payment_schema.py").write_text("TOKEN_HEADER = 'X-Pay-HMAC-SHA256'\n", encoding="utf-8")

    prompts_seen = []

    class SpyRouter(ScriptedRouter):
        def choose(self, state_text, question, options, ctx=None):
            raise AssertionError("描述類問題不應交給 Decision-1 choose")

        def write(self, prompt, effort="low", ctx=None):
            prompts_seen.append((prompt, effort))
            return "請使用 shared_C/payment_schema.py 定義的 X-Pay-HMAC-SHA256 標頭。"

    router = SpyRouter()
    res = route_question(
        router,
        goal=GOAL,
        title="請說明 payment_schema.py 的標頭格式",
        detail="需要知道 shared_C/payment_schema.py 定義了什麼標頭？",
        options=[],
        reference_root=str(parent_a),
        worktree=str(sub_b),
    )
    assert res["handoff"] is False
    assert res["kind"] == "descriptive"
    assert res["by"] == "GLM-5.3"
    assert "X-Pay-HMAC-SHA256" in res["answer_text"]
    assert len(prompts_seen) == 1
    sent_prompt, effort = prompts_seen[0]
    assert effort == "normal"
    assert "TOKEN_HEADER = 'X-Pay-HMAC-SHA256'" in sent_prompt


def test_streak_cap_and_destructive_question_escalate_to_user():
    router = ScriptedRouter()
    # 超過連續提問上限 -> handoff=True 交給使用者
    over = route_question(
        router, goal=GOAL, title="一般問題", detail="請說明欄位用途", options=[], streak=MAX_QUESTION_STREAK
    )
    assert over["handoff"] is True
    assert over["by"] == "human"

    # 破壞性問題 -> 一律交給使用者
    assert is_destructive_question("資料庫清理", "是否可以執行 drop database 並刪除整個正式環境資料？")
    dest = route_question(
        router, goal=GOAL, title="資料庫清理", detail="是否可以執行 drop database？", options=[]
    )
    assert dest["handoff"] is True
    assert dest["by"] == "human"
