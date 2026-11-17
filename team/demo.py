"""示範沙盒：不需要真 CLI、不連網，用假代理跑完一整間房（spec → 計畫 → 實作 → 測試 → 審查 → 等你合併）。

- 沙盒 repo：<base_dir>/sandbox/demo-<n>（calc.py 只有 add、test_calc.py、progress.md「目前任務：加入 mul」）。
- 假代理劇本放在 repo 外面（<base_dir>/sandbox/.agents/demo-<n>/），不弄髒主 repo，合併時才不會被擋。
- 每個座位（kiro、agy、claude、glm、glm_high）都指向假代理，自動派工時也不需要真 CLI。
"""
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional

from team import gitops
from team.rooms import Room

FAKES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakes")
AGENT = os.path.join(FAKES, "fake_acp_agent.py")
FAKE_CLAUDE = os.path.join(FAKES, "fake_claude.py")
SLUG = "add-mul"
DOCS = "docs/ai-team/%s" % SLUG
GOAL = "在 calc.py 加入 mul(a, b) 乘法函式，並在 test_calc.py 補上測試"

CALC_ADD = "def add(a, b):\n    return a + b\n"
TEST_ADD = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
CALC_MUL = CALC_ADD + "\n\ndef mul(a, b):\n    return a * b\n"
TEST_MUL = ("from calc import add, mul\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
            "def test_mul():\n    assert mul(2, 3) == 6\n")
_DIR_LOCK = threading.Lock()  # 同時建兩間示範房時不會拿到同一個編號
PROGRESS = "# 示範專案進度\n\n目前任務：加入 mul\n\n- [x] add\n- [ ] mul\n"


def _py() -> str:
    return sys.executable


def spec_turn() -> List[Dict[str, Any]]:
    py = _py()
    spec = {
        "criteria": [{"id": "C1", "text": "calc 加入 mul(a, b)",
                      "check": '"%s" -c "from calc import mul; assert mul(2, 3) == 6"' % py}],
        "test_cmd": '"%s" -m pytest -q -p no:cacheprovider' % py,
        "allowed_paths": ["calc.py", "test_calc.py"],
    }
    return [{"say": "我讀完專案了，驗收條件如下。\n```json\n%s\n```" % json.dumps(spec, ensure_ascii=False)}]


def plan_turn() -> List[Dict[str, Any]]:
    return [{"write": {"path": DOCS + "/plan.md", "content": "# 計畫\n1. 加入 mul 與測試（對應 C1）\n"}},
            {"commit": "plan"}, {"say": "DONE"}]


def build_turn(msg: str = "C1: mul") -> List[Dict[str, Any]]:
    return [{"write": {"path": "calc.py", "content": CALC_MUL}},
            {"write": {"path": "test_calc.py", "content": TEST_MUL}},
            {"tool": {"title": "跑測試", "kind": "execute", "rawInput": {"command": "python -m pytest -q"}}},
            {"commit": msg}, {"say": "DONE"}]


def review_ok() -> List[Dict[str, Any]]:
    return [{"say": '審查完成。\n```json\n{"blocking":[],"suggestions":["可再補邊界測試"]}\n```'}]


def default_scripts() -> Dict[str, List[List[Dict[str, Any]]]]:
    """座位 → 劇本（turn 的 list）。實作劇本同 Task 10 happy path；審查回一則建議。"""
    impl = [spec_turn(), plan_turn(), build_turn()]
    return {
        "kiro": impl,
        "agy": [review_ok()] * 4,
        "claude": impl,
        "glm": [build_turn("C1: mul (glm)")] * 3,
        "glm_high": [review_ok()] * 4,
    }


def _next_dir(sandbox: str) -> int:
    n = 1
    while os.path.exists(os.path.join(sandbox, "demo-%d" % n)) or \
            os.path.exists(os.path.join(sandbox, ".agents", "demo-%d" % n)):
        n += 1
    return n


def _make_repo(repo: str) -> None:
    files = {"calc.py": CALC_ADD, "test_calc.py": TEST_ADD, "progress.md": PROGRESS,
             ".gitignore": "__pycache__/\n.pytest_cache/\n"}
    for name, text in files.items():
        with open(os.path.join(repo, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    gitops.git(repo, "init", "-q")
    gitops.git(repo, "symbolic-ref", "HEAD", "refs/heads/main")
    gitops.git(repo, "add", "-A")
    gitops.git(repo, *gitops.IDENTITY, "commit", "-q", "--no-verify",
               "-m", "示範專案：calc 只有 add")


def create_demo_room(office: Any, base_dir: str,
                     scripts: Optional[Dict[str, List[List[Dict[str, Any]]]]] = None,
                     autonomy: str = "balanced") -> Room:
    """建立沙盒 repo 與假代理劇本，派一間示範房（demo=True、實作 kiro、審查 agy）。

    scripts 可覆蓋個別座位的劇本（測試用，例如讓 kiro 回「用量已滿」）。"""
    sandbox = os.path.join(os.path.abspath(base_dir), "sandbox")
    os.makedirs(sandbox, exist_ok=True)
    with _DIR_LOCK:
        n = _next_dir(sandbox)
        repo = os.path.join(sandbox, "demo-%d" % n)
        agents = os.path.join(sandbox, ".agents", "demo-%d" % n)
        os.makedirs(agents)
        os.makedirs(repo)
    _make_repo(repo)

    all_scripts = default_scripts()
    all_scripts.update(scripts or {})
    py = _py()
    overrides = {}  # type: Dict[str, List[str]]
    for seat, script in all_scripts.items():
        path = os.path.join(agents, "%s.json" % seat)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(script, f, ensure_ascii=False, indent=1)
        if seat in ("kiro", "agy"):
            overrides[seat] = [py, AGENT, path, os.path.join(agents, "%s.state" % seat)]
        else:
            overrides[seat] = [py, FAKE_CLAUDE, "--script", path]
    return office._create_room(repo=repo, goal=GOAL, source="progress.md：加入 mul", impl_cli="kiro",
                               review_cli="agy", autonomy=autonomy, allowed_paths=[], name="示範 %d" % n,
                               demo=True, slug=SLUG, cmd_overrides=overrides)
