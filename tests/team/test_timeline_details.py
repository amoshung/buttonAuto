"""Task 19：時間軸可展開的細節（工具指令與輸出、模型的題目與答案、督促原文與送出訊息）；細節裡不得有金鑰。"""
import json
import sys
import os

from team.events import AgentEvent, PermissionRequest, clip, mask_text, tool_detail, tool_result
from team.models import ModelConfig, ModelReply, ModelRouter

from test_runner import BUILD_TURN, D1_SECRET, PLAN_TURN, SECRET, SPEC_TURN, Env, repo  # noqa: F401


def raw_events(env):
    return open(os.path.join(env.store.root, env.room.id, "events.jsonl"), encoding="utf-8").read()


def test_helpers_clip_mask_and_shapes():
    assert clip("x" * 10, 4) .startswith("xxxx") and "省略 6 字" in clip("x" * 10, 4)
    assert clip("abcdefgh", 3, tail=True).endswith("fgh")
    assert mask_text("token=%s ok" % SECRET, [SECRET]) == "token=*** ok"
    assert mask_text("export API_KEY=abcdefghijkl", []) == "export API_KEY=***"
    d = tool_detail("Run Command", {"kind": "execute", "rawInput": {"command": "pytest -q", "path": "calc.py", "timeout": 30},
                                    "locations": [{"path": "test_calc.py"}]})
    assert d["command"] == "pytest -q" and d["paths"] == ["calc.py", "test_calc.py"] and '"timeout": 30' in d["input"]
    r = tool_result({"status": "failed", "content": [{"type": "content", "content": {"type": "text", "text": "1 failed"}}]})
    assert r == {"status": "failed", "output": "1 failed"}
    assert tool_result({"status": "completed", "rawOutput": {"stdout": "ok", "stderr": "warn"}})["output"] == "ok\nwarn"


def test_tool_permission_prompt_and_nudge_details_have_no_secrets(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    r = env.runner
    r._on_event("Kiro", AgentEvent("tool", "Run Command", {
        "toolCallId": "t9", "kind": "execute", "rawInput": {"command": "echo %s && pytest -q" % SECRET, "cwd": "/w"}}))
    r._on_event("Kiro", AgentEvent("tool_update", "", {"toolCallId": "t9", "status": "in_progress"}))
    r._on_event("Kiro", AgentEvent("tool_update", "", {
        "toolCallId": "t9", "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "key %s\n2 passed" % D1_SECRET}}]}))
    evs = env.events()
    tool = [e for e in evs if e["kind"] == "tool"][-1]
    assert tool["tool_call_id"] == "t9" and tool["detail"]["command"] == "echo *** && pytest -q"
    assert tool["detail"]["title"] == "Run Command" and '"cwd": "/w"' in tool["detail"]["input"]
    res = [e for e in evs if e["kind"] == "tool_result"]
    assert len(res) == 1 and res[0]["tool_call_id"] == "t9" and res[0]["text"] == "Run Command：完成"
    assert res[0]["detail"] == {"status": "completed", "output": "key ***\n2 passed"}
    # 督促：模型原文＋實際送出
    r.room = env.get()
    sent = r._nudge()
    nudge = [e for e in env.events() if e["kind"] == "say" and e.get("nudge")][-1]
    assert nudge["detail"]["generated"].startswith("（假模型回覆）") and nudge["detail"]["sent"] == sent
    assert "目標（全文）" in nudge["detail"]["sent"]
    # 權限：指令、判斷數字、模型
    req = PermissionRequest("t10", "Run Command", "execute", {"command": "python -m pytest -q", "path": "calc.py"},
                            [{"optionId": "a", "name": "允許", "kind": "allow_once"},
                             {"optionId": "r", "name": "拒絕", "kind": "reject_once"}])
    r._wt = type("W", (), {"path": str(repo)})()
    r._on_permission(req, False, "Kiro")
    perm = [e for e in env.events() if e["kind"] == "permission"][-1]
    assert perm["detail"]["command"] == "python -m pytest -q" and perm["detail"]["decision"] == "allow"
    assert perm["detail"]["reason"] and perm["detail"]["paths"] == ["calc.py"] and perm["detail"]["allow"] == 0.95
    raw = raw_events(env)
    assert SECRET not in raw and D1_SECRET not in raw


def test_happy_path_tool_events_carry_command(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    room = env.run()
    assert room.status == "done", room.error
    tools = [e for e in env.events() if e["kind"] == "tool" and e["text"] == "跑測試"]
    assert tools and tools[0]["detail"]["command"] == "python -m pytest -q" and tools[0]["tool_call_id"]
    done = [e for e in env.events() if e["kind"] == "tool_result" and e["tool_call_id"] == tools[0]["tool_call_id"]]
    assert done and done[0]["detail"]["status"] == "completed"
    prompts = [e for e in env.events() if e["kind"] == "prompt"]
    assert prompts and all(len(p["detail"]["sent"]) >= len(p["text"]) for p in prompts)
    env.no_secrets()


def test_model_call_detail_question_answer_threshold():
    cfg = ModelConfig("http://x.invalid", SECRET, D1_SECRET)
    calls = []
    r = ModelRouter(cfg, d1=lambda s, q: {"q": {"noul": 0.18}},
                    glm=lambda p, e, t: ModelReply("請先完成 C1，再 commit。key=%s" % SECRET, None, "glm-5.3"))
    r.listeners.append(calls.append)
    p = r.probability("狀態：%s 已完成 0 個 commit" % D1_SECRET, "代理是否還在朝目標前進？",
                      ctx={"room_id": "r1", "role": "scrum", "purpose": "判斷是否偏離目標", "pass_min": 0.5})
    assert p == 0.18
    d = calls[-1].detail
    assert "代理是否還在朝目標前進？" in d["question"] and d["state_excerpt"].startswith("狀態：***")
    assert "0.18" in d["answer"] and d["threshold"] == 0.5 and d["verdict"] == "0.18 < 0.50 不通過"
    text = r.write("寫一則督促訊息", ctx={"room_id": "r1", "role": "scrum", "purpose": "寫督促訊息"})
    d = calls[-1].detail
    assert d["question"] == "寫一則督促訊息" and d["answer"] == "請先完成 C1，再 commit。key=***" and SECRET in text
    assert SECRET not in json.dumps([c.detail for c in calls], ensure_ascii=False)
    assert D1_SECRET not in json.dumps([c.detail for c in calls], ensure_ascii=False)


# ---- fix round 1 ----
def test_headless_tool_result_completes_the_tool(tmp_path):
    from team.headless import HeadlessClaude
    got = []
    h = HeadlessClaude([sys.executable], str(tmp_path), got.append, read_only=False)
    state = {"used": None, "size": None, "result": None}
    h._handle({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q"}}]}}, state)
    h._handle({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "2 passed"}]},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "boom", "is_error": True}]}}, state)
    ups = [e for e in got if e.kind == "tool_update"]
    assert [(u.data["toolCallId"], u.data["status"]) for u in ups] == [("toolu_1", "completed"), ("toolu_2", "failed")]
    assert tool_result(ups[0].data) == {"status": "completed", "output": "2 passed"}
    assert tool_result(ups[1].data)["output"] == "boom"


def test_updates_merge_fields_and_keys_are_namespaced(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    r = env.runner
    s1 = {"seq": 1, "drv": None}
    s2 = {"seq": 2, "drv": None}
    r._on_event("Kiro", AgentEvent("tool", "Run", {"toolCallId": "t1", "kind": "execute"}), s1)
    # 更新帶來 rawInput／title／locations：只補有的欄位
    r._on_event("Kiro", AgentEvent("tool_update", "", {"toolCallId": "t1", "status": "in_progress",
                                                      "rawInput": {"command": "pytest -q"}, "title": "Run pytest",
                                                      "locations": [{"path": "tests/test_calc.py"}]}), s1)
    r._on_event("Kiro", AgentEvent("tool_update", "", {"toolCallId": "t1", "status": "in_progress"}), s1)  # 沒有新欄位 → 不寫
    # 另一個對話（換新對話後）同樣的 t1：不同的合併鍵
    r._on_event("Kiro", AgentEvent("tool", "Read", {"toolCallId": "t1", "kind": "read"}), s2)
    r._on_event("Kiro", AgentEvent("tool_update", "", {"toolCallId": "t1", "status": "completed",
                                                      "content": [{"type": "content", "content": {"type": "text", "text": "x"}}]}), s1)
    evs = env.events()
    tools = [e for e in evs if e["kind"] == "tool"]
    assert len({e["tool_key"] for e in tools}) == 2
    upd = [e for e in evs if e["kind"] == "tool_update"]
    assert len(upd) == 1 and upd[0]["tool_key"] == tools[0]["tool_key"]
    assert upd[0]["detail"] == {"title": "Run pytest", "command": "pytest -q", "paths": ["tests/test_calc.py"]}
    res = [e for e in evs if e["kind"] == "tool_result"][-1]
    assert res["tool_key"] == tools[0]["tool_key"] and res["status"] == "completed" and res["text"] == "Run pytest：完成"


def test_mask_before_clip_and_more_key_shapes(tmp_path, repo):  # noqa: F811
    env = Env(tmp_path, repo, [SPEC_TURN, PLAN_TURN, BUILD_TURN])
    r = env.runner
    # 金鑰剛好跨在 300 字截斷點上：先遮再截，不會留下半段
    text = "x" * 290 + SECRET + " rest"
    r._event("prompt", r._mclip(text, 300), detail={"sent": r._mclip(text, 300)})
    for tok in ("GITHUB_TOKEN=ghp_abcdefghijklmnop", "AUTH_TOKEN: zzzzzzzzzzzz", "MY_SERVICE_TOKEN=qqqqqqqqqqqq",
                "github_pat_11ABCDEFG0123456789", "sk-proj-abcdefghijklmn"):
        r._on_event("Kiro", AgentEvent("tool", "Run " + tok, {"toolCallId": "k", "rawInput": {"command": "echo " + tok}}))
    raw = raw_events(env)
    for leak in (SECRET[:12], "ghp_abcdefghijklmnop", "zzzzzzzzzzzz", "qqqqqqqqqqqq", "11ABCDEFG0123456789", "proj-abcdefghijklmn"):
        assert leak not in raw, leak
    # 看得到的摘要文字也遮
    tool = [e for e in env.events() if e["kind"] == "tool"][-1]
    assert "***" in tool["text"]


def test_snapshot_and_room_api_drop_details_and_rotation(tmp_path):
    from team.rooms import RoomStore, slim_event
    store = RoomStore(str(tmp_path / "t"))
    room = store.create(name="p", repo=str(tmp_path), slug="s", goal="g", source="goal", impl_cli="auto", review_cli="auto")
    store.event(room.id, "tool", "Run", tool_call_id="t", detail={"command": "x" * 3000})
    e = store.events(room.id, limit=1)[0]
    assert e["id"] and slim_event(e).get("detail") is None and slim_event(e)["has_detail"] is True
    assert store.events_by_id(room.id, [e["id"]])[0]["detail"]["command"] == "x" * 3000
    # 輪替：只留最後 cap 行，舊的進 gz
    store.events_cap, store.events_slack = 50, 10
    for i in range(80):
        store.event(room.id, "notice", "n%d" % i)
    left = store.events(room.id, limit=1000)
    assert len(left) <= 60 and left[0]["text"] == "n79"
    assert os.path.exists(os.path.join(store.root, room.id, "events-1.jsonl.gz"))


# ---- fix round 2 ----
def test_mask_is_linear_and_token_names_are_whole_words():
    import time
    for text in ("a" * 200000, "a_" * 100000, "token" * 40000, "x-" * 100000, "secret" * 40000):
        t0 = time.perf_counter()
        mask_text(text, [SECRET])
        assert time.perf_counter() - t0 < 0.5, text[:10]
    assert mask_text("tokenizer_config.json") == "tokenizer_config.json"
    assert mask_text("load tokenizer_config.json from tokenizers/x") == "load tokenizer_config.json from tokenizers/x"
    assert mask_text("MY_SERVICE_TOKEN=qqqqqqqqqqqq") == "MY_SERVICE_TOKEN=***"
    # 超長輸入：中間省略，頭尾都遮到
    big = "api_key=abcdefghijkl " + "x" * 500000 + " " + SECRET
    out = mask_text(big, [SECRET])
    assert "abcdefghijkl" not in out and SECRET not in out and "中間省略" in out
