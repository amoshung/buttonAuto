import json

from ai_client import (
    AIError,
    DecisionClient,
    LLMDecisionClient,
    build_llm_decision_prompt,
    extract_json,
    parse_llm_decision,
)
from ai_settings import AISettings
from automation import (
    build_decision_questions,
    check_deny,
    build_choice_questions,
    clean_reply,
    crop_to_panel,
    decide_choice,
    ground_with_uia,
    snap_to_source,
    decide_plan,
    make_clients,
    normalize_options,
)
from config import AppConfig
from uia_driver import ButtonInfo, name_in, normalize_name, pick_button
import win_utils as wu


def _answers(action, conf=0.95, risk=0.05, button=None, b_conf=0.9):
    a = {"action": {"choice": action, "confidence": conf}, "risk": {"noul": risk}}
    if button is not None:
        a["button"] = {"choice": button, "confidence": b_conf}
    return a


def test_normalize_and_match_button_names():
    assert normalize_name("Submit ↵") == "submit"
    assert normalize_name("Run (Ctrl+Enter)") == "run"
    assert normalize_name("\uea70 View changes (5)") == "view changes (5)"
    assert name_in("Always allow", ["always ALLOW"])
    assert not name_in("Allow all", ["Allow"])
    assert not name_in("", ["Allow"])


def test_pick_button_prefers_panel_then_lowest():
    bs = [
        ButtonInfo("Allow", (0, 100, 50, 120), "outside"),
        ButtonInfo("Allow", (600, 300, 650, 320), "panel_upper"),
        ButtonInfo("Allow", (600, 800, 650, 820), "panel_lower"),
    ]
    assert pick_button(bs, "allow", panel_rect=(500, 0, 1000, 1000)).control == "panel_lower"
    assert pick_button(bs, "allow").control == "panel_lower"
    assert pick_button(bs, "Reject") is None


def test_check_deny_patterns():
    pats = AppConfig().deny_patterns
    assert check_deny(["git push --force origin main"], pats)
    assert check_deny(["rm -rf build/"], pats)
    assert check_deny(["Remove-Item .\\out -Recurse -Force"], pats)
    assert check_deny(["DROP TABLE users;"], pats)
    assert check_deny(["git status", "python -m pytest"], pats) is None
    assert check_deny(["x"], ["([bad"]) is None  # 無效的正規表示式會被忽略


def test_decide_plan_click_and_thresholds():
    cfg = AppConfig()
    vs = {"action_buttons": ["Allow", "Reject"]}
    plan = decide_plan(vs, _answers("click_button", button="opt_0"), cfg, has_input=True)
    assert plan.kind == "click" and plan.button == "Allow"

    assert decide_plan(vs, _answers("click_button", risk=0.8, button="opt_0"), cfg, True).kind == "ask_human"
    assert decide_plan(vs, _answers("click_button", conf=0.3, button="opt_0"), cfg, True).kind == "ask_human"
    assert decide_plan(vs, _answers("click_button", button="opt_0", b_conf=0.2), cfg, True).kind == "ask_human"
    # Reject 在永不點擊清單中
    assert decide_plan(vs, _answers("click_button", button="opt_1"), cfg, True).kind == "ask_human"
    # 選項編號超出範圍
    assert decide_plan(vs, _answers("click_button", button="opt_9"), cfg, True).kind == "ask_human"


def test_decide_plan_wait_continue_reply_and_missing_risk():
    cfg = AppConfig()
    assert decide_plan({}, _answers("wait", risk=0.9), cfg, True).kind == "wait"
    p = decide_plan({}, _answers("continue"), cfg, True)
    assert p.kind == "continue" and p.text == cfg.ai_continue_text
    assert decide_plan({}, _answers("continue"), cfg, has_input=False).kind == "ask_human"
    assert decide_plan({}, _answers("reply"), cfg, True).kind == "reply"
    cfg.ai_allow_reply = False
    assert decide_plan({}, _answers("reply"), cfg, True).kind == "ask_human"
    # Decision-1 沒回風險分數時視為高風險
    assert decide_plan({}, {"action": {"choice": "continue", "confidence": 0.99}}, AppConfig(), True).kind == "ask_human"


def test_decision_questions_shape():
    q = build_decision_questions(["Allow", "Always allow"])
    assert q["action"]["type"] == "choice" and "ask_human" in q["action"]["criteria"]
    assert q["risk"]["type"] == "noul"
    assert list(q["button"]["criteria"]) == ["opt_0", "opt_1"]
    assert "button" not in build_decision_questions([])


def test_extract_json_and_clean_reply():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('結果：{"agent_state": "working"} 完畢') == {"agent_state": "working"}
    assert extract_json("not json") is None
    assert clean_reply('「好的，請用方案 A」') == "好的，請用方案 A"
    assert clean_reply("```\n繼續\n```") == "繼續"


def test_text_inputs_use_shift_enter_for_newlines():
    seq = wu.build_text_inputs("ab\nc", send_key="enter")
    # a,b,c 各按下+放開 = 6；換行 Shift+Enter = 4；送出 Enter = 2
    assert len(seq) == 12
    assert seq[-2].u.ki.wVk == wu.VK_RETURN
    assert len(wu.build_text_inputs("x", send_key="none")) == 2
    assert len(wu.build_text_inputs("😀", send_key="none")) == 4  # 代理對 = 兩個 UTF-16 單位


def test_ai_settings_file_and_manual_keys(tmp_path):
    (tmp_path / "k1.md").write_text("  moa_abc \n", encoding="utf-8")
    s = AISettings()
    s.vision.key_file = str(tmp_path / "k1.md")
    s.vision.key = "manual_key"
    assert s.vision_key() == "moa_abc"
    s.key_source = "manual"
    assert s.vision_key() == "manual_key"
    s.decision.key_file = str(tmp_path / "missing.md")
    s.key_source = "file"
    assert s.decision_key() == ""

    path = tmp_path / "secrets.json"
    s.save(str(path))
    loaded = AISettings.load(str(path))
    assert loaded.vision.key_file == str(tmp_path / "k1.md")


def test_llm_decision_prompt_and_parse():
    qs = build_decision_questions(["Allow", "Reject"])
    prompt = build_llm_decision_prompt("代理要求執行 pytest", qs)
    assert "click_button" in prompt and "opt_1" in prompt and "pytest" in prompt

    raw = ('```json\n{"action": {"choice": "click_button", "confidence": 0.9},'
           ' "risk": {"noul": 0.05}, "button": {"choice": "opt_0", "confidence": 1.7}}\n```')
    ans = parse_llm_decision(raw, qs)
    assert ans["action"] == {"type": "choice", "choice": "click_button", "confidence": 0.9}
    assert ans["risk"]["noul"] == 0.05
    assert ans["button"]["confidence"] == 1.0  # 超出範圍會被夾到 0~1
    plan = decide_plan({"action_buttons": ["Allow", "Reject"]}, ans, AppConfig(), True)
    assert plan.kind == "click" and plan.button == "Allow"

    # 不認識的選項、缺少風險分數 → 對應題目被省略 → 結果偏向交給人工
    bad = parse_llm_decision('{"action": {"choice": "nuke", "confidence": 1}, "risk": {}}', qs)
    assert bad == {}
    partial = parse_llm_decision('{"action": {"choice": "continue", "confidence": 0.99}}', qs)
    assert decide_plan({}, partial, AppConfig(), True).kind == "ask_human"


def test_llm_decision_client_uses_chat(monkeypatch):
    class FakeChat:
        model = "gemma-4-31b"

        def __init__(self):
            self.calls = []

        def chat(self, prompt, image_png=None, max_tokens=0, temperature=None):
            self.calls.append((prompt, temperature))
            return '{"ok": {"noul": 0.8}}'

    fake = FakeChat()
    ans = LLMDecisionClient(fake).systemone("測試", {"ok": {"type": "noul", "instructions": "?"}})
    assert ans["ok"]["noul"] == 0.8 and fake.calls[0][1] == 0

    fake.chat = lambda *a, **k: "我不知道"
    try:
        LLMDecisionClient(fake).systemone("測試", {"ok": {"type": "noul", "instructions": "?"}})
        assert False, "應該丟出 AIError"
    except AIError:
        pass


def test_make_clients_backend_switch():
    s = AISettings()
    s.vision.base_url, s.key_source, s.vision.key = "http://x", "manual", "k"
    vision, judge, decision = make_clients(s)
    assert vision.model == "gemma-4-31b" and judge.model == "gemma-4-31b"
    assert isinstance(decision, LLMDecisionClient) and decision.chat_client is judge
    s.decision_backend = "decision1"
    assert isinstance(make_clients(s)[2], DecisionClient)


def _choice_answers(best, conf=0.7, risks=(0.05, 0.05, 0.05, 0.05)):
    a = {"best": {"choice": best, "confidence": conf}}
    for i, r in enumerate(risks):
        a[f"risk_{i}"] = {"noul": r}
    return a


OPTS = {"options": [
    {"label": "1", "text": "使用 Alembic 自動產生遷移腳本", "button": ""},
    {"label": "2", "text": "手寫 SQL 遷移檔並人工審查", "button": ""},
    {"label": "3", "text": "先備份資料再重建資料表", "button": ""},
    {"label": "4", "text": "暫緩遷移", "button": ""},
]}


def test_normalize_options_tolerates_bad_input():
    assert normalize_options({}) == []
    assert normalize_options({"options": "A"}) == []
    got = normalize_options({"options": ["甲", {"text": ""}, {"label": "B", "button": "Use B"}]})
    assert got == [{"label": "", "text": "甲", "button": ""}, {"label": "B", "text": "Use B", "button": "Use B"}]
    assert len(normalize_options({"options": [str(i) for i in range(20)]})) == 8


def test_choice_questions_one_risk_per_option():
    q = build_choice_questions(normalize_options(OPTS))
    assert list(q["best"]["criteria"]) == ["opt_0", "opt_1", "opt_2", "opt_3"]
    assert "2. 手寫 SQL" in q["best"]["criteria"]["opt_1"]
    assert all(f"risk_{i}" in q for i in range(4))


def test_decide_choice_types_label_or_clicks_button():
    cfg = AppConfig()
    opts = normalize_options(OPTS)
    p = decide_choice(opts, _choice_answers("opt_1"), cfg, has_input=True)
    assert p.kind == "choose" and p.text == "2"

    no_label = [dict(o, label="") for o in opts]
    assert decide_choice(no_label, _choice_answers("opt_0"), cfg, True).text == "使用 Alembic 自動產生遷移腳本"

    btn = [{"label": "A", "text": "Keep REST", "button": "A. Keep REST"}, {"label": "B", "text": "GraphQL", "button": "B. GraphQL"}]
    p = decide_choice(btn, _choice_answers("opt_1", risks=(0.1, 0.1)), cfg, has_input=False)
    assert p.kind == "click" and p.button == "B. GraphQL"


def test_decide_choice_falls_back_to_human():
    cfg = AppConfig()
    opts = normalize_options(OPTS)
    assert decide_choice(opts, _choice_answers("opt_2", conf=0.3), cfg, True).kind == "ask_human"
    assert decide_choice(opts, _choice_answers("opt_2", risks=(0.0, 0.0, 0.9, 0.0)), cfg, True).kind == "ask_human"
    assert decide_choice(opts, _choice_answers("opt_7"), cfg, True).kind == "ask_human"
    assert decide_choice(opts, {"best": {"choice": "opt_0", "confidence": 0.9}}, cfg, True).kind == "ask_human"  # 缺風險分數
    assert decide_choice(opts, _choice_answers("opt_0"), cfg, has_input=False).kind == "ask_human"
    never = [{"label": "", "text": "Delete", "button": "Delete"}, {"label": "", "text": "Keep", "button": "Keep"}]
    assert decide_choice(never, _choice_answers("opt_0", risks=(0.0, 0.0)), cfg, True).kind == "ask_human"


def test_pick_button_fuzzy_for_long_option_text():
    bs = [ButtonInfo("A. Keep the existing REST endpoints", (0, 0, 10, 10), "a"),
          ButtonInfo("B. Migrate everything to GraphQL", (0, 20, 10, 30), "b")]
    assert pick_button(bs, "B. Migrate everything to GraphQl.") is None
    assert pick_button(bs, "B. Migrate everything to GraphQl.", fuzzy=True).control == "b"
    assert pick_button(bs, "C. gRPC", fuzzy=True) is None


PANEL = """我需要執行以下指令來完成這一步：
Get-ChildItem -Recurse -Filter "*.log" | Where-Object { $_.Length -gt 10MB } |
  Remove-Item -WhatIf
Reject
Allow
設定檔格式有四種做法，請回覆選項編號：
1) 將 config.json 改為 YAML 格式並保留舊檔相容
2) 維持 JSON，新增 JSON Schema 驗證"""


def test_snap_to_source_fixes_ocr_typos():
    ocr = 'Get-ChildItem -Recurse -Filter "*.log" | Where-Object { $_.Length -gt 10 } Remove-Item -WhatIf'
    fixed = snap_to_source(ocr, PANEL)
    assert "10MB } |" in fixed and fixed.endswith("Remove-Item -WhatIf")
    # 面板裡找不到相近的文字 → 保留 OCR 結果
    assert snap_to_source("npm run build", PANEL) == "npm run build"
    assert snap_to_source("x", "") == "x"


def test_ground_with_uia_fixes_command_and_options():
    vs = {
        "command": 'Get-ChildItem -Recurse -Filter "*.log" | Where-Object { $_.Length -gt 10 Remove-Item -WhatIf',
        "options": [{"label": "1", "text": "將 config.json 改為 YAML 格式並保留舊相容"},
                    {"label": "2", "text": "維持 JSON ，新增 JSON Schema 驗證"}],
    }
    changed = ground_with_uia(vs, PANEL)
    assert changed == ["command", "options[0]", "options[1]"]
    assert "10MB" in vs["command"]
    assert vs["options"][0]["text"] == "將 config.json 改為 YAML 格式並保留舊檔相容"
    assert vs["options"][1]["text"] == "維持 JSON，新增 JSON Schema 驗證"
    assert ground_with_uia({"command": "ls"}, "") == []


def test_crop_to_panel(monkeypatch):
    import numpy as np
    monkeypatch.setattr(wu, "get_window_rect", lambda h: (100, 50, 1100, 850))
    rgb = np.zeros((800, 1000, 3), dtype=np.uint8)
    assert crop_to_panel(rgb, 1, (700, 60, 1090, 840)).shape == (780, 390, 3)
    assert crop_to_panel(rgb, 1, None) is rgb
    assert crop_to_panel(rgb, 1, (700, 60, 750, 100)) is rgb  # 太小就用整窗


def test_config_new_fields_and_old_file_compat(tmp_path):
    old = tmp_path / "config.json"
    old.write_text(json.dumps({"scan_interval": 0.5, "threshold": 0.9, "targets": []}), encoding="utf-8")
    cfg = AppConfig.load(str(old))
    assert cfg.scan_interval == 0.5 and cfg.targets == []
    assert cfg.mode == "ai" and cfg.ai_dry_run is True and cfg.authorized_sessions == []
    assert any(a["match"] == "Kiro.exe" for a in cfg.watch_apps)

    cfg.mode = "ai"
    cfg.ai_max_risk = 0.2
    cfg.save(str(old))
    again = AppConfig.load(str(old))
    assert again.mode == "ai" and again.ai_max_risk == 0.2

    old.write_text(json.dumps({"mode": "bogus"}), encoding="utf-8")
    assert AppConfig.load(str(old)).mode == "ai"


def test_ai_never_clicks_always_allow():
    cfg = AppConfig()
    vs = {"action_buttons": ["Deny", "Allow", "Always allow"]}
    p = decide_plan(vs, _answers("click_button", button="opt_2"), cfg, has_input=True)
    assert p.kind == "click" and p.button == "Allow"  # 改點一次性的 Allow
    only_always = {"action_buttons": ["Always allow"]}
    assert decide_plan(only_always, _answers("click_button", button="opt_0"), cfg, True).kind == "ask_human"


def test_check_deny_negated_mentions_in_outgoing_prompts():
    pats = AppConfig().deny_patterns
    assert check_deny(["請在功能分支 commit，不要 git push"], pats, allow_negated=True) is None
    assert check_deny(["禁止執行 rm -rf build"], pats, allow_negated=True) is None
    assert check_deny(["do not git push to origin"], pats, allow_negated=True) is None
    assert check_deny(["請執行 git push origin main"], pats, allow_negated=True) is None  # 一般 push 允許
    assert check_deny(["不要 force push。接著 git push --force origin main"], pats, allow_negated=True)  # 後面那句是真的要 force push
    assert check_deny(["不要 git rm a.py"], pats)  # 代理要執行的指令不考慮否定


def test_extract_json_tolerates_paths_and_raw_newlines():
    raw = '```json\n{"agent_state": "waiting_approval", "command": "$ev = \'D:\\_AI_Projs\\sandbox\'\nGet-ChildItem $ev",}\n```'
    obj = extract_json(raw)
    assert obj["agent_state"] == "waiting_approval"
    assert "D:\\_AI_Projs\\sandbox" in obj["command"] and "\n" in obj["command"]


def test_decide_approval_paths():
    from automation import decide_approval, one_time_allow_button
    cfg = AppConfig()
    btns = ["Deny", "Always allow", "Allow", "Always deny"]
    assert one_time_allow_button(btns) == "Allow"
    assert one_time_allow_button(["Always allow", "Deny"]) is None
    ans = lambda allow, risk: {"allow": {"noul": allow}, "risk": {"noul": risk}}
    p, need = decide_approval(ans(0.95, 0.1), None, btns, cfg)
    assert p.kind == "click" and p.button == "Allow" and not need
    p, need = decide_approval(ans(0.95, 0.5), None, btns, cfg)
    assert p.kind == "wait" and need  # 灰色地帶：先拿第二意見
    p, _ = decide_approval(ans(0.95, 0.5), (0.1, True, "只讀取檔案"), btns, cfg)
    assert p.kind == "click" and "第二意見" in p.reason
    p, _ = decide_approval(ans(0.95, 0.5), (0.6, True, "會刪檔"), btns, cfg)
    assert p.kind == "ask_human"
    p, _ = decide_approval(ans(0.95, 0.5), (0.1, False, "範圍外"), btns, cfg)
    assert p.kind == "ask_human"
    assert decide_approval(ans(0.95, 0.9), None, btns, cfg)[0].kind == "ask_human"
    assert decide_approval(ans(0.1, 0.5), None, btns, cfg)[0].kind == "ask_human"
    assert decide_approval(ans(0.3, 0.1), None, btns, cfg)[1] is True  # 不確定但風險低：先拿第二意見
    assert decide_approval({}, None, btns, cfg)[0].kind == "ask_human"  # 缺資料一律保守
    assert decide_approval(ans(0.95, 0.1), None, ["Always allow"], cfg)[0].kind == "ask_human"


def test_decide_plan_uses_p_human_not_top_confidence():
    cfg = AppConfig()
    spread = {"action": {"choice": "click_button", "confidence": 0.45,
                         "probabilities": {"click_button": 0.45, "continue": 0.35, "reply": 0.1, "ask_human": 0.05,
                                           "wait": 0.05}},
              "risk": {"noul": 0.05}, "button": {"choice": "opt_0", "confidence": 0.9}}
    p = decide_plan({"action_buttons": ["Submit"]}, spread, cfg, True)
    assert p.kind == "click" and p.scores["p_human"] == 0.05
    worried = dict(spread, action={"choice": "click_button", "confidence": 0.5,
                                   "probabilities": {"click_button": 0.5, "ask_human": 0.45}})
    assert decide_plan({"action_buttons": ["Submit"]}, worried, cfg, True).kind == "ask_human"


def test_service_trouble_detection():
    from automation import is_service_trouble
    for m in ["視覺模型回應 429：Too Many Requests", "視覺模型連線失敗：Read timed out", "Decision-1 回應 503",
              "HTTPConnectionPool: Max retries exceeded"]:
        assert is_service_trouble(m)
    assert not is_service_trouble("判斷模型回覆無法解析")


def test_same_text_ignores_kiro_markdown_formatting():
    from uia_driver import same_text
    sent = "請把結果寫到這個檔案（Markdown）：`D:\\a\\draft.md`，寫好後回覆「已寫入」。\n1. 條件：… | 驗證方式：…"
    kiro = "請把結果寫到這個檔案（Markdown）D:\\a\\draft.md，寫好後回覆「已寫入」。1. 條件… | 驗證方式…"
    assert same_text(kiro, sent)
    assert not same_text("請只回覆收到，，。", "請只回覆「收到」，")  # 輸入法打亂順序仍會被抓到
    assert not same_text("請把結果寫到", sent)  # 漏字仍會被抓到


def test_asks_clear():
    from automation import asks_clear
    assert asks_clear("context 已接近 60%，請執行 /clear。/clear 之後告訴我「繼續」")
    assert not asks_clear("我會先清掉暫存檔再繼續")


def test_text_prompt_lists_panel_buttons_and_unknown_fallback():
    from automation import build_text_prompt, AGENT_STATES
    p = build_text_prompt("Kiro", "代理問：要繼續嗎？", ["Allow", "Deny"])
    assert "[1] Allow" in p and "[2] Deny" in p and '"unknown"' in p and '"agent_state"' in p
    assert "unknown" not in AGENT_STATES  # 回 unknown 就會改用截圖


def test_roles_from_model_pick_allow_once_not_keywords():
    from automation import approvals_from_roles, decide_approval, parse_control_roles
    from config import AppConfig
    controls = ["Yes, allow this time", "Yes, and always allow 'Get-ChildItem' in this conversation",
                "Yes, and always allow 'Get-ChildItem'", "No (tell the agent what to do instead)", "Skip",
                "Add context"]
    vs = {"controls": [{"id": 1, "role": "allow_once"}, {"id": 2, "role": "allow_always"},
                       {"id": 3, "role": "allow_always"}, {"id": 4, "role": "deny"}, {"id": 5, "role": "skip"},
                       {"id": 6, "role": "other"}, {"id": 99, "role": "allow_once"}]}
    roles = parse_control_roles(vs, controls)
    assert len(roles) == 6
    approvals = approvals_from_roles(roles, controls, ["Skip"])
    assert approvals == controls[:5]
    answers = {"allow": {"noul": 1.0}, "risk": {"noul": 0.0}}
    plan, _ = decide_approval(answers, None, approvals, AppConfig(), roles)
    assert plan.kind == "click" and plan.button == "Yes, allow this time"


def test_model_mislabel_of_persistent_option_is_still_refused():
    from automation import decide_approval
    from config import AppConfig
    roles = {"Yes, and always allow 'rm'": "allow_once", "No": "deny"}
    plan, _ = decide_approval({"allow": {"noul": 1.0}, "risk": {"noul": 0.0}}, None, list(roles), AppConfig(), roles)
    assert plan.kind == "ask_human"


def test_question_card_answers_are_not_approvals():
    from automation import approvals_from_roles
    controls = ["Use PostgreSQL", "Use SQLite", "Skip"]
    roles = {"Use PostgreSQL": "answer", "Use SQLite": "answer", "Skip": "skip"}
    assert approvals_from_roles(roles, controls, controls) == []
    assert approvals_from_roles({}, controls, ["Skip"]) == ["Skip"]  # 模型沒標時才退回名稱比對


def test_ask_option_name_keeps_original_text():
    import uia_driver as ud
    assert ud.ask_option_name("1 Yes, allow this time") == "Yes, allow this time"
    assert ud.ask_option_name("4 No (tell the agent what to do instead) Record voice memo") == \
        "No (tell the agent what to do instead)"


def test_cancel_alone_is_not_an_approval():
    from automation import approvals_from_roles
    assert approvals_from_roles({"Cancel": "deny", "Add context": "other"}, ["Cancel", "Add context"], []) == []
    assert approvals_from_roles({"Allow": "other", "Deny": "deny"}, ["Allow", "Deny"], ["Allow", "Deny"]) == ["Allow", "Deny"]
