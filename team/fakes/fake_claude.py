"""假 claude（headless stream-json，只用標準庫）。

認得的參數：-p [prompt]、--permission-mode <mode>、--resume <id>、--model <m>、--allowedTools <樣式…>；其他忽略。
-p 後面沒有提示詞（下一個是選項或沒有）→ 從 stdin 讀 UTF-8 提示詞（真的 claude 也這樣）。
回覆文字附上 chars=<提示詞字數>。
acceptEdits 模式模擬 headless 權限：劇本的 commit 只有在 --allowedTools 含 Bash(git add:*) 與
Bash(git commit:*) 時才執行，否則回「權限被拒」（真的 claude 在 -p 模式會默默拒絕要核准的 Bash）。
輸出：system/init、assistant text、assistant tool_use、result（含 usage、total_cost_usd 與 modelUsage，形狀同真的 claude）。
不是 plan 模式時在 cwd 寫入 claude_was_here.txt。

測試用環境變數：
- FAKE_CLAUDE_FAIL=1：stderr 寫「not logged in」並 exit 3，不輸出 result。
- FAKE_CLAUDE_IS_ERROR=1：result 的 is_error 為真。
- FAKE_CLAUDE_SLEEP=<秒>：init 之後先睡（測逾時與取消）。
- FAKE_CLAUDE_NO_RESULT=1：不輸出 result，exit 0。
- FAKE_CLAUDE_LINGER=<秒>：輸出 result 後再睡才結束。
- FAKE_CLAUDE_SHOW_ENV=A,B：assistant text 附上這些變數是否存在（只印 <set>／<unset>，不印值）。

劇本模式（--script <劇本路徑>）：劇本是 turn 的 list，每次執行（一個回合）跑下一個 turn，
turn 編號存在「劇本路徑.turn」。step：say、write {path,content}、commit <訊息>、run <shell 指令>、
echo_prompt、error <訊息>（result is_error）。plan 模式不執行 write／commit。劇本模式不寫 claude_was_here.txt。
"""
import json
import os
import subprocess
import sys
import time


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=fake-agent", "-c", "user.email=fake@local", *args],
                          cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def allowed_tools(argv):
    """--allowedTools 後面到下一個選項為止的樣式（可變長度參數）。"""
    out = []
    i = 0
    while i < len(argv):
        if argv[i] == "--allowedTools":
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                out.append(argv[i])
                i += 1
            continue
        i += 1
    return out


def bash_allowed(mode, allowed, *patterns):
    if mode in ("bypassPermissions",):
        return True
    return all(p in allowed for p in patterns)


def run_script(path, prompt, mode, emit_text, allowed=()):
    """跑劇本的下一個 turn；回傳 error 訊息或 None。"""
    with open(path, encoding="utf-8") as f:
        script = json.load(f)
    state = path + ".turn"
    idx = 0
    try:
        with open(state, encoding="utf-8") as f:
            idx = int(f.read().strip() or 0)
    except (OSError, ValueError):
        pass
    with open(state, "w", encoding="utf-8") as f:
        f.write(str(idx + 1))
    if not script:
        return None
    cwd = os.getcwd()
    for step in script[min(idx, len(script) - 1)]:
        if "error" in step:
            return str(step["error"])
        if "say" in step:
            emit_text(step["say"])
        elif "echo_prompt" in step:
            emit_text(prompt[:200])
        elif mode == "plan":
            continue
        elif "write" in step:
            w = step["write"]
            full = os.path.join(cwd, w["path"])
            os.makedirs(os.path.dirname(full) or cwd, exist_ok=True)
            with open(full, "w", encoding="utf-8", newline="") as f:
                f.write(w.get("content", ""))
        elif "commit" in step:
            if not bash_allowed(mode, allowed, "Bash(git add:*)", "Bash(git commit:*)"):
                emit_text("（權限被拒：git commit 需要核准）")
                continue
            _git(cwd, "add", "-A")
            r = _git(cwd, "commit", "-m", step["commit"])
            if r.returncode != 0:
                emit_text("（commit 失敗：%s）" % (r.stderr or r.stdout).strip())
        elif "run" in step:
            r = subprocess.run(step["run"], shell=True, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            emit_text("$ %s\n%s(exit %s)" % (step["run"], r.stdout + r.stderr, r.returncode))
    return None


def out(msg):
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def result_usage(model):
    """result 行的用量欄位（真的 claude 的形狀）：usage、total_cost_usd、modelUsage（以模型 id 為鍵）。"""
    return {"usage": {"input_tokens": 100, "cache_creation_input_tokens": 300, "cache_read_input_tokens": 2000,
                      "output_tokens": 50},
            "total_cost_usd": 0.0123,
            "modelUsage": {model: {"inputTokens": 100, "outputTokens": 50, "cacheReadInputTokens": 2000,
                                   "cacheCreationInputTokens": 300, "costUSD": 0.0123,
                                   "contextWindow": 200000}}}


def arg(argv, name, default=""):
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    argv = sys.argv[1:]
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        sys.stderr.write("not logged in\n")
        sys.exit(3)
    prompt = arg(argv, "-p")
    if "-p" in argv and (not prompt or prompt.startswith("--")):
        prompt = sys.stdin.buffer.read().decode("utf-8")
    allowed = allowed_tools(argv)
    mode = arg(argv, "--permission-mode", "default")
    session_id = arg(argv, "--resume") or "fake-claude-1"
    model = arg(argv, "--model") or os.environ.get("ANTHROPIC_MODEL", "") or "claude-default"
    out({"type": "system", "subtype": "init", "session_id": session_id,
         "cwd": os.getcwd(), "model": model, "permissionMode": mode})
    sleep = float(os.environ.get("FAKE_CLAUDE_SLEEP") or 0)
    if sleep:
        time.sleep(sleep)
    usage = {"input_tokens": 100, "cache_read_input_tokens": 2000,
             "cache_creation_input_tokens": 300, "output_tokens": 50}
    script = arg(argv, "--script")
    if script:
        def emit_text(t):
            out({"type": "assistant", "session_id": session_id, "message": {
                "role": "assistant", "model": model, "usage": usage,
                "content": [{"type": "text", "text": t}]}})
        err = run_script(script, prompt, mode, emit_text, allowed)
        out(dict(result_usage(model), type="result", subtype="error_during_execution" if err else "success",
                 is_error=bool(err), session_id=session_id, result=err or "完成"))
        return
    text = "收到：%s（model=%s）chars=%d" % (prompt[:200], model, len(prompt))
    for name in filter(None, os.environ.get("FAKE_CLAUDE_SHOW_ENV", "").split(",")):
        text += " %s=%s" % (name, "<set>" if name in os.environ else "<unset>")
    out({"type": "assistant", "session_id": session_id, "message": {
        "role": "assistant", "model": model, "usage": usage,
        "content": [{"type": "text", "text": text}]}})
    out({"type": "assistant", "session_id": session_id, "message": {
        "role": "assistant", "model": model, "usage": usage,
        "content": [{"type": "tool_use", "id": "toolu_1", "name": "Write",
                     "input": {"file_path": "claude_was_here.txt", "content": "hi"}}]}})
    if mode != "plan":
        with open(os.path.join(os.getcwd(), "claude_was_here.txt"), "w",
                  encoding="utf-8") as f:
            f.write("hi")
    if os.environ.get("FAKE_CLAUDE_NO_RESULT"):
        return
    is_error = bool(os.environ.get("FAKE_CLAUDE_IS_ERROR"))
    out(dict(result_usage(model), type="result", subtype="error_during_execution" if is_error else "success",
             is_error=is_error, session_id=session_id, result="假錯誤" if is_error else "完成"))
    linger = float(os.environ.get("FAKE_CLAUDE_LINGER") or 0)
    if linger:
        time.sleep(linger)


if __name__ == "__main__":
    main()
