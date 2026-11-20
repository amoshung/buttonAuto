"""ACP 偵察：檢查 Kiro CLI（kiro-cli）與 Antigravity CLI（agy）有沒有安裝、能不能用 ACP 交握。

只做 ACP 的 initialize 交握（問對方支援什麼、要怎麼登入），不開對話、不送任何任務、不改任何設定，
做完立刻結束子程序。結果寫到 recon\\acp-<時間>.json 與畫面上。
"""
import datetime
import glob
import json
import os
import queue
import shutil
import subprocess
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
LOCAL = os.environ.get("LOCALAPPDATA", os.path.join(HOME, "AppData", "Local"))

CANDIDATES = {
    "agy": ["agy", os.path.join(LOCAL, "agy", "bin", "agy.exe"), os.path.join(LOCAL, "Programs", "antigravity-cli", "agy.exe"),
            os.path.join(LOCAL, "antigravity-cli", "agy.exe"), os.path.join(HOME, ".local", "bin", "agy.exe"),
            os.path.join(HOME, ".antigravity", "bin", "agy.exe")],
    "kiro-cli": ["kiro-cli", os.path.join(LOCAL, "Programs", "kiro-cli", "kiro-cli.exe"),
                 os.path.join(LOCAL, "kiro-cli", "bin", "kiro-cli.exe"), os.path.join(HOME, ".local", "bin", "kiro-cli.exe"),
                 os.path.join(HOME, ".kiro", "bin", "kiro-cli.exe")],
    "claude": ["claude", os.path.join(HOME, ".local", "bin", "claude.exe"),
               os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd")],
}


def find(name):
    for c in CANDIDATES[name]:
        p = shutil.which(c) if not os.path.isabs(c) else (c if os.path.isfile(c) else None)
        if p:
            return p
    for pat in (os.path.join(LOCAL, "Programs", "**", name + ".exe"), os.path.join(HOME, ".*", "**", name + ".exe")):
        hits = glob.glob(pat, recursive=True)[:1]
        if hits:
            return hits[0]
    return None


def run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace",
                           shell=cmd[0].lower().endswith((".cmd", ".bat")),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"code": p.returncode, "out": (p.stdout or "")[-3000:], "err": (p.stderr or "")[-2000:]}
    except Exception as e:
        return {"error": repr(e)}


def acp_initialize(cmd, timeout=40):
    """啟動 ACP server，送一個 initialize，讀回應後結束。"""
    out = {"cmd": cmd}
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=BASE_DIR, shell=cmd[0].lower().endswith((".cmd", ".bat")),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        return {"cmd": cmd, "error": repr(e)}
    lines: "queue.Queue[str]" = queue.Queue()
    err_lines = []

    def reader(stream, sink):
        for raw in iter(stream.readline, b""):
            sink(raw.decode("utf-8", "replace").rstrip("\r\n"))

    threading.Thread(target=reader, args=(proc.stdout, lines.put), daemon=True).start()
    threading.Thread(target=reader, args=(proc.stderr, err_lines.append), daemon=True).start()
    req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
           "params": {"protocolVersion": 1,
                      "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                      "clientInfo": {"name": "ai-engineering-assistant-probe", "version": "0.1"}}}
    try:
        proc.stdin.write((json.dumps(req) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except Exception as e:
        out["write_error"] = repr(e)
    deadline = time.monotonic() + timeout
    other = []
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            other.append(line[:300])
            continue
        if msg.get("id") == 1:
            out["response"] = msg
            break
        other.append(msg)
    else:
        out["timeout"] = True
    out["other_output"] = other[:20]
    out["stderr_tail"] = err_lines[-30:]
    try:
        proc.kill()
    except Exception:
        pass
    return out


def main():
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.join(BASE_DIR, "recon"), exist_ok=True)
    result = {"time": stamp, "tools": {}}
    for name, acp_args, ver_args in (("agy", ["acp"], ["--version"]), ("kiro-cli", ["acp"], ["--version"]),
                                     ("claude", None, ["--version"])):
        print(f"== {name}", flush=True)
        path = find(name)
        info = {"path": path}
        if not path:
            print("   找不到執行檔（不在 PATH，也不在常見安裝位置）", flush=True)
            result["tools"][name] = info
            continue
        print(f"   位置：{path}", flush=True)
        info["version"] = run([path] + ver_args)
        print(f"   版本：{(info['version'].get('out') or info['version'].get('err') or '').strip()[:120]}", flush=True)
        if acp_args is None:  # Claude CLI 沒有內建 ACP：只確認 headless 參數
            info["help"] = run([path, "--help"])
            ok = "--print" in (info["help"].get("out") or "") or "-p" in (info["help"].get("out") or "")
            print(f"   {'✅' if ok else '⚠'} headless（-p）{'可用' if ok else '沒看到'}", flush=True)
            result["tools"][name] = info
            continue
        info["acp_help"] = run([path, "acp", "--help"])
        if name == "kiro-cli":
            info["whoami"] = run([path, "whoami"])
        print("   ACP 交握中（最多 40 秒）…", flush=True)
        info["acp"] = acp_initialize([path] + acp_args)
        resp = info["acp"].get("response") or {}
        if "result" in resp:
            r = resp["result"]
            print(f"   ✅ ACP 可用：協定版本 {r.get('protocolVersion')}，登入方式 "
                  f"{[m.get('id') or m.get('name') for m in r.get('authMethods', [])]}", flush=True)
        else:
            print(f"   ❌ ACP 沒有回應：{json.dumps(resp or info['acp'], ensure_ascii=False)[:300]}", flush=True)
        result["tools"][name] = info
    path = os.path.join(BASE_DIR, "recon", f"acp-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"完成：{path}", flush=True)


if __name__ == "__main__":
    main()
