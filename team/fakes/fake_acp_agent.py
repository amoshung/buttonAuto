"""劇本式假 ACP 代理（只用標準庫）。

用法: python fake_acp_agent.py [劇本路徑 [狀態檔路徑]]；沒有參數則讀環境變數 FAKE_ACP_SCRIPT。
劇本是 turn 的 list，每個 turn 是 step 的 list。

step：say、usage {used,size[,cost:{amount,currency}]}、info {usagePercentage}（kiro 的 session_info_update
_meta.kiro.contextUsage）、echo_prompt、tool {title,kind,rawInput}、write {path,content}、
commit <訊息>、run <shell 指令>、error <訊息>（本回合以 JSON-RPC 錯誤回應 session/prompt，之後的 step 不執行）。
環境變數：FAKE_ACP_MODEL＝session/new 回傳 models.currentModelId；FAKE_ACP_MODELS＝逗號分隔的可用模型（availableModels，
並接受 session/set_model 切到其中之一）；FAKE_ACP_CONFIG＝early|late：改用 Kiro v3 的 config_option_update 回報模型、接受 session/set_config_option；
FAKE_ACP_LOG＝把 session/new、set_model、set_config_option、session/prompt 記到這個檔（測試用）；FAKE_ACP_PROMPT_USAGE=1＝session/prompt 的結果附
usage {inputTokens, outputTokens, totalTokens}。都沒設時跟真的 kiro 一樣不回報模型與 token。
狀態檔：記錄下一個要跑的 turn 編號，讓同一份劇本在新程序（新 session）接著跑（測試換手用）。
"""
import json
import os
import queue
import subprocess
import sys
import threading

SAFE_KINDS = {"read", "search", "think", "fetch"}
OPTIONS = [
    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]

_out_lock = threading.Lock()


def send(msg):
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _out_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


class Agent:
    def __init__(self, script, state_path=""):
        self.script = script
        self.state_path = state_path
        self.turn_index = 0
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, encoding="utf-8") as f:
                    self.turn_index = int(f.read().strip() or 0)
            except (OSError, ValueError):
                pass
        self.cwd = os.getcwd()
        self.session_id = "fake-1"
        self.cancel = threading.Event()
        self.proc = None
        self.proc_lock = threading.Lock()
        self.replies = {}  # 我方發出的 request id -> Queue
        self.next_id = 1
        self.id_lock = threading.Lock()
        self.worker = None
        self.tool_n = 0

    # ---- 輸出輔助 ----
    def update(self, upd):
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": self.session_id, "update": upd}})

    def say(self, text):
        self.update({"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": text}})

    def request_permission(self, tool_call):
        with self.id_lock:
            rid = self.next_id
            self.next_id += 1
        q = queue.Queue()
        self.replies[rid] = q
        send({"jsonrpc": "2.0", "id": rid, "method": "session/request_permission",
              "params": {"sessionId": self.session_id, "toolCall": tool_call,
                         "options": OPTIONS}})
        while not self.cancel.is_set():
            try:
                res = q.get(timeout=0.1)
            except queue.Empty:
                continue
            self.replies.pop(rid, None)
            outcome = (res or {}).get("outcome") or {}
            if outcome.get("outcome") != "selected":
                return False
            return str(outcome.get("optionId", "")).startswith("allow")
        self.replies.pop(rid, None)
        return False

    # ---- step 執行 ----
    def run_git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=fake-agent", "-c", "user.email=fake@local", *args],
            cwd=self.cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)

    def run_turn(self, prompt_text):
        """執行一個 turn；遇到 error step 回傳錯誤訊息，否則回 None。"""
        if not self.script:
            return None
        idx = min(self.turn_index, len(self.script) - 1)
        self.turn_index += 1
        if self.state_path:
            with open(self.state_path, "w", encoding="utf-8") as f:
                f.write(str(self.turn_index))
        rejected = False
        for step in self.script[idx]:
            if self.cancel.is_set():
                return None
            if "error" in step:
                return str(step["error"])
            if "say" in step:
                self.say(step["say"])
            elif "say_report" in step:
                rep = dict(step["say_report"])
                sha = self.run_git("rev-parse", "--short", "HEAD").stdout.strip() or "HEAD"
                crit = []
                for c in rep.get("criteria") or []:
                    c = dict(c)
                    if c.get("commit") in (None, "", "HEAD"):
                        c["commit"] = sha
                    crit.append(c)
                if crit:
                    rep["criteria"] = crit
                self.say("DONE\n```json\n%s\n```" % json.dumps(rep, ensure_ascii=False))
            elif "usage" in step:
                u = step["usage"]
                upd = {"sessionUpdate": "usage_update", "used": u.get("used", 0), "size": u.get("size", 0)}
                if "cost" in u:
                    upd["cost"] = u["cost"]
                self.update(upd)
            elif "info" in step:
                self.update({"sessionUpdate": "session_info_update",
                             "_meta": {"kiro": {"contextUsage": dict(step["info"])}}})
            elif "echo_prompt" in step:
                self.say(prompt_text[:200])
            elif "tool" in step:
                t = step["tool"]
                self.tool_n += 1
                tid = "t%d" % self.tool_n
                title = t.get("title", "")
                kind = t.get("kind", "other")
                raw = t.get("rawInput", {})
                self.update({"sessionUpdate": "tool_call", "toolCallId": tid,
                             "title": title, "kind": kind, "status": "pending",
                             "rawInput": raw})
                allowed = True
                if kind not in SAFE_KINDS:
                    allowed = self.request_permission(
                        {"toolCallId": tid, "title": title, "kind": kind, "rawInput": raw})
                    if self.cancel.is_set():
                        return None
                if allowed:
                    self.update({"sessionUpdate": "tool_call_update",
                                 "toolCallId": tid, "status": "completed"})
                else:
                    self.update({"sessionUpdate": "tool_call_update",
                                 "toolCallId": tid, "status": "failed"})
                    self.say("（被拒絕：%s）" % title)
                    rejected = True
            elif rejected:
                continue  # 被拒絕後略過 write/commit/run
            elif "write" in step:
                w = step["write"]
                path = os.path.join(self.cwd, w["path"])
                os.makedirs(os.path.dirname(path) or self.cwd, exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(w.get("content", ""))
            elif "commit" in step:
                self.run_git("add", "-A")
                r = self.run_git("commit", "-m", step["commit"])
                if r.returncode != 0:
                    self.say("（commit 失敗：%s）" % (r.stderr or r.stdout).strip())
            elif "run" in step:
                self.run_shell(step["run"])
        return None

    def run_shell(self, cmd):
        with self.proc_lock:
            if self.cancel.is_set():
                return
            kw = {}
            if os.name != "nt":
                kw["start_new_session"] = True
            self.proc = subprocess.Popen(
                cmd, shell=True, cwd=self.cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", **kw)
        out, _ = self.proc.communicate()
        code = self.proc.returncode
        self.proc = None
        if self.cancel.is_set():
            return
        self.say("$ %s\n%s(exit %s)" % (cmd, out or "", code))

    def kill_proc(self):
        with self.proc_lock:
            p = self.proc
            if p and p.poll() is None:
                try:
                    if os.name != "nt":
                        import signal
                        os.killpg(p.pid, signal.SIGKILL)
                    else:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                                       capture_output=True)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    def prompt_worker(self, rid, prompt_text):
        err = None
        try:
            err = self.run_turn(prompt_text)
            reason = "cancelled" if self.cancel.is_set() else "end_turn"
        except Exception as e:  # 不讓 worker 靜默死掉
            self.say("（假代理錯誤：%s）" % e)
            reason = "end_turn"
        if err is not None and not self.cancel.is_set():
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": err}})
            return
        res = {"stopReason": reason}
        if os.environ.get("FAKE_ACP_PROMPT_USAGE"):
            res["usage"] = {"inputTokens": 1500, "outputTokens": 120, "totalTokens": 1620}
        send({"jsonrpc": "2.0", "id": rid, "result": res})

    # ---- 訊息分派 ----
    def handle(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        if method is None:
            # 對我方 request 的回覆
            q = self.replies.get(rid)
            if q is not None:
                q.put(msg.get("result") or {})
            return
        params = msg.get("params") or {}
        if method == "session/cancel":
            self.cancel.set()
            self.kill_proc()
            return
        if rid is None:
            return  # 其他通知忽略
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1, "agentCapabilities": {"loadSession": False},
                "authMethods": []}})
        elif method == "session/new":
            self.cwd = params.get("cwd") or self.cwd
            res = {"sessionId": self.session_id}
            ids = [x for x in os.environ.get("FAKE_ACP_MODELS", "").split(",") if x]
            cfg_mode = os.environ.get("FAKE_ACP_CONFIG", "")
            if cfg_mode and ids:
                # Kiro v3：不放 models，改送 config_option_update（early＝在回應前、late＝回應後 0.5 秒）
                note = {"jsonrpc": "2.0", "method": "session/update", "params": {
                    "sessionId": self.session_id, "update": {
                        "sessionUpdate": "config_option_update", "configOptions": [
                            {"type": "select", "id": "mode", "category": "mode", "currentValue": "default",
                             "options": [{"value": "default", "name": "Default"}]},
                            {"type": "select", "id": "model", "category": "model", "currentValue": ids[0],
                             "options": [{"value": m, "name": m.upper(), "description": "v3 %s" % m}
                                         for m in ids]}]}}}
                if cfg_mode == "late":
                    def later():
                        import time
                        time.sleep(0.5)
                        send(note)
                    threading.Thread(target=later, daemon=True).start()
                else:
                    send(note)
            elif os.environ.get("FAKE_ACP_MODEL") or ids:
                cur = os.environ.get("FAKE_ACP_MODEL") or ids[0]
                res["models"] = {"currentModelId": cur,
                                 "availableModels": [{"modelId": m, "name": m.upper(), "description": "fake %s" % m}
                                                     for m in (ids or [cur])]}
            log_new = os.environ.get("FAKE_ACP_LOG")
            if log_new:
                with open(log_new, "a", encoding="utf-8") as f:
                    f.write("session/new\n")
            send({"jsonrpc": "2.0", "id": rid, "result": res})
        elif method == "session/set_config_option" and os.environ.get("FAKE_ACP_CONFIG"):
            ok = (params.get("configId") == "model"
                  and params.get("value") in os.environ.get("FAKE_ACP_MODELS", "").split(","))
            if os.environ.get("FAKE_ACP_LOG"):
                with open(os.environ["FAKE_ACP_LOG"], "a", encoding="utf-8") as f:
                    f.write("set_config_option %s\n" % params.get("value"))
            if ok:
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "invalid value"}})
        elif method == "session/set_model" and os.environ.get("FAKE_ACP_MODELS") and not os.environ.get(
                "FAKE_ACP_CONFIG"):
            mid = params.get("modelId")
            if mid in os.environ["FAKE_ACP_MODELS"].split(","):
                if os.environ.get("FAKE_ACP_LOG"):
                    with open(os.environ["FAKE_ACP_LOG"], "a", encoding="utf-8") as f:
                        f.write("set_model %s\n" % mid)
                send({"jsonrpc": "2.0", "id": rid, "result": {}})
            else:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown model"}})
        elif method == "session/prompt":
            if os.environ.get("FAKE_ACP_LOG"):
                with open(os.environ["FAKE_ACP_LOG"], "a", encoding="utf-8") as f:
                    f.write("session/prompt\n")
            if self.worker and self.worker.is_alive():
                self.cancel.set()
                self.kill_proc()
                self.worker.join(5)
            self.cancel.clear()
            text = "".join(b.get("text", "") for b in params.get("prompt", [])
                           if isinstance(b, dict))
            self.worker = threading.Thread(
                target=self.prompt_worker, args=(rid, text), daemon=True)
            self.worker.start()
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": "Method not found: %s" % method}})


def main():
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FAKE_ACP_SCRIPT")
    state_path = sys.argv[2] if len(sys.argv) > 2 and not sys.argv[2].startswith("-") else ""
    script = []
    if path:
        with open(path, encoding="utf-8") as f:
            script = json.load(f)
    agent = Agent(script, state_path)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        agent.handle(msg)
    # EOF: 結束進行中的工作後乾淨離開
    agent.cancel.set()
    agent.kill_proc()
    if agent.worker:
        agent.worker.join(5)


if __name__ == "__main__":
    main()
