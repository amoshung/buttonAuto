---
title: Compound-Engineering Role Playbooks, Question Routing, and Subfolder Write Scope - Plan
type: feat
date: 2026-11-18
artifact_contract: ce-unified-plan/v1
artifact_readiness: completed
status: completed
product_contract_source: ce-brainstorm
execution: code
---

# Compound-Engineering Role Playbooks, Question Routing, and Subfolder Write Scope - Plan

## Goal Capsule

- **Objective:** Ensure every room verifies and provisions `compound-engineering` before work starts, binds role-specific CE workflows across all CLIs and models (including `GLM-5.3`), routes multiple-choice questions to `Decision-1` and descriptive questions to `GLM-5.3`, and isolates all git/write operations to a user-selected target subfolder `B` while keeping parent folder `A` and sibling subfolders `C/D/E` as strictly read-only reference material.
- **Means:** Local git and in-place `.ai-team-worktrees` initialization inside `B` (`KTD1`) with read-only `A` permission and post-turn integrity guards (`KTD2`), pre-flight CE skill provisioning plus dual-layer role prompt injection (`KTD3`), and a two-branch question router dispatching option sets to `Decision-1` and open-ended prompts to `GLM-5.3` via `ModelRouter` (`KTD4`).
- **Authority hierarchy:** Product Contract `R1`–`R12` govern behavior; Planning Contract `KTD1`–`KTD4` govern technical mechanism; Implementation Units `U1`–`U4` own file-level changes.
- **Stop conditions:** Any write or git operation mutating parent folder `A` or sibling folders `C/D/E`; any model/CLI role lacking explicit CE workflow rules; any fallback to banned local models (Ollama/gemma/muse-glimmer).
- **Execution profile:** Test-backed Python backend + Next.js static export (`web/pixel`).

---

## Product Contract

**Product Contract preservation:** Product Contract unchanged from confirmed `ce-brainstorm` scoping synthesis.

### Summary

Equip every room with pre-flight `compound-engineering` (CE) verification and provisioning, explicit role-level CE capability bindings across all CLIs (`claude`, `kiro`, `agy`) and `GLM-5.3` seats (`glm`, `glm_high`), automated question routing (`Decision-1` for multiple-choice questions and `GLM-5.3` for descriptive questions), and subfolder write scoping where parent folder `A` and sibling folders `C/D/E` remain read-only reference sources while all git/worktree and write operations occur strictly inside target subfolder `B`.

### Problem Frame

Users frequently open a parent workspace folder `A` containing multiple sibling directories (`B`, `C`, `D`, `E`, `F`, `G`), intending to modify only target project `B` while consulting `C/D/E` as read-only reference material. Previously, the room runner required `A` itself to be a Git repository, placed `.ai-team-worktrees` in the parent directory of the repo, lacked a `subdirs` field in `team/scan.py` and a UI selector in the dispatch wizard to pick `B`, and only checked root-escape at runtime rather than enforcing read-only access on `A` and write-only access on `B`. In addition, rooms did not verify or mount `compound-engineering` before starting work, did not map CE skills (`ce-brainstorm`, `ce-plan`, `ce-work`, `ce-debug`, `ce-simplify-code`, `ce-code-review`, `ce-compound`) to each room role across both CLI seats and `GLM-5.3`, and only delegated fixed system-inbox retry menus rather than routing mid-work multiple-choice questions to `Decision-1` and descriptive questions to `GLM-5.3`.

### Key Decisions

- **Lock Git and Worktree initialization to target subfolder `B` itself, never touching `A` or `C/D/E`** (session-settled: user-directed — chosen over creating worktrees at parent folder `A`: prevents polluting parent folder `A` or sibling reference folders when `B` lacks git or sits inside a multi-project workspace). Governs R1, R2, R3, R4.
- **Provision `compound-engineering` from local canonical skills first with online fallback, while redirecting CE planning/compound artifacts to `{notes_dir}` and forbidding auto-push/PR** (session-settled: user-approved — chosen over unguarded stock CE output paths and online-only install: preserves intranet reliability and keeps repo working trees clean until user merge approval). Governs R5, R6, R7, R8.
- **Bind CE capabilities via dual-layer configuration (CLI plugin/skill mounting + role-scoped CE playbook prompt injection) across all CLIs and `GLM-5.3`** (session-settled: user-directed — chosen over CLI plugin flag mounting alone: guarantees consistent CE methodology across `claude`, `kiro`, `agy`, and `GLM-5.3` seats). Governs R6, R7, R8.
- **Enable automatic question routing by default (`Decision-1` for multiple-choice, `GLM-5.3` for descriptive) with a 3-turn streak cap and destructive-action escalation** (session-settled: user-approved — chosen over manual-only inbox pausing or unbounded loops: maintains autonomous velocity while preventing infinite question loops). Governs R9, R10, R11, R12.

### Requirements

**Subfolder Write Scope & Read-Only Reference Isolation (`A` vs. `B`)**

- R1. In `team/scan.py:scan_project` and the Dispatch Wizard (`pixeloffice/components/DispatchWizard.tsx`), scanning or selecting a folder `A` must return and display its discovered first-level subfolders (`subdirs`) so the user can select a target subfolder `B` as the exclusive writable workspace (or leave the root folder `A` as the target when working on a standalone project), while badging unselected sibling folders (`C, D, E, F, G`) as read-only reference sources.
- R2. When target subfolder `B` is selected under parent folder `A`, the room must store `B` (`A/B`) as the active target repository (`repo`) and `A` as the read-only reference root (`reference_root`), never running `git init`, `git worktree`, or file writes in `A` or sibling folders `C/D/E`.
- R3. Before creating a room worktree for target `B`, if `B` does not yet have its own local Git repository or initial commit, the system must automatically initialize a local Git repository inside `B` (`git init` + initial baseline commit of existing files or `--allow-empty`) and create the room worktree inside `B/.ai-team-worktrees/<slug>` (excluding `.ai-team-worktrees/` via `.git/info/exclude`) so all worktree files reside inside `B` itself.
- R4. Real-time permission checks (`team/policy.py:decide`), CLI launch flags (`--add-dir` for `HeadlessClaude`), and post-turn checks (`team/checks.py`) must allow read/search tools across both `B`'s worktree and `reference_root` (`A` including `C/D/E`), while denying any write, edit, delete, or mutating shell operation targeting paths outside `B`'s worktree (and verifying post-turn that `reference_root` outside `B` remains unmodified).

**Pre-Work `compound-engineering` Verification & Cross-CLI/Model Role Binding**

- R5. Before any room begins execution in `RoomRunner._run()`, the system must verify whether `compound-engineering` skills (`ce-brainstorm`, `ce-strategy`, `ce-plan`, `ce-doc-review`, `ce-work`, `ce-debug`, `ce-simplify-code`, `ce-test-browser`, `ce-code-review`, `ce-compound`) are installed for the active environment and CLI seats (`claude`, `kiro`, `agy`); if missing, it must automatically provision/sync them from the local canonical skill directory (`~/.gemini/config/skills/ce-*` or configured plugin directory) first, falling back to CLI/git installation if no local source exists.
- R6. Every pipeline role (`po`, `tech_lead`, `scrum`, `implementer`, `fixer`, `qa`, `reviewer`) must receive an explicit `compound-engineering` capability configuration through both CLI skill/plugin mounting (`--plugin-dir` for `HeadlessClaude` seats `claude`/`glm`/`glm_high`, `~/.gemini/config/skills/ce-*` for `agy`, and `~/.kiro/skills/ce-*` for `kiro`) and role-scoped CE playbook instructions injected directly via `team/personas.py:Composer` and `team/prompt_defaults.py` so every CLI model and `GLM-5.3` execute the exact CE methodology for their role.
- R7. Developer roles must follow CE development workflows (`po` -> `ce-brainstorm` + `ce-strategy`; `tech_lead` / `scrum` -> `ce-plan` + `ce-doc-review`; `implementer` -> `ce-work` + `ce-simplify-code`; `fixer` -> `ce-debug` + `ce-work`), and reviewer/QA roles must follow CE review workflows (`qa` -> `ce-work` verification gate + `ce-test-browser`; `reviewer` -> `ce-code-review` + `ce-compound`).
- R8. All CE role playbooks must enforce room boundary rules: planning and compound-learning documents must be written to `{notes_dir}` (never polluting the target repository `B`), and agents must never execute `git push`, `gh pr create`, or merge into the main branch without user approval at the room merge gate.

**Question Routing (`Decision-1` vs. `GLM-5.3`)**

- R9. When any agent role asks a question during execution — either via an ACP `session/request_permission` prompt carrying question options, a room inbox question, or by emitting a question / `NEEDS_CONTEXT` output at turn end — the system must classify whether the question is **multiple-choice (選項類問題)** (contains 2 or more distinct options/choices) or **descriptive (描述類問題)** (open-ended request for context, design clarification, or reference behavior).
- R10. Multiple-choice questions must be routed to **`Decision-1`** via `ModelRouter.choose` / `ModelRouter.probability` in `team/models.py`, scoring each option against the room's development goal (`room.goal`) and approved spec (`{notes_dir}/spec.md`) to select the option least likely to deviate from the development goal.
- R11. Descriptive questions must be routed to **`GLM-5.3`** via `ModelRouter.write` in `team/models.py`, which synthesizes a concrete answer grounded in `room.goal`, `{notes_dir}/spec.md`, `{notes_dir}/plan.md`, and relevant read-only files from `reference_root` (`A`).
- R12. Automated question routing must be active by default for rooms (`delegate = True` default on creation) and must track consecutive question turns per stage; if the same stage asks questions more than 3 consecutive times without progress, or if a question involves destructive/out-of-scope operations, the question must escalate to the user's Inbox (`waiting_user`).

### Scope Boundaries

- **In scope:** Subfolder `B` discovery (`team/scan.py`) and selection in Dispatch Wizard, local git + in-place worktree creation inside `B`, read-only reference access to `A` (`C/D/E`), pre-flight `compound-engineering` check/provisioning, explicit Role × CE capability matrix across all CLIs and `GLM-5.3`, and `Decision-1` (multiple-choice) vs. `GLM-5.3` (descriptive) question routing via `ModelRouter`.
- **Out of scope:** Managing multiple unrelated parent folders (`A1`, `A2`) simultaneously in a single room.

---

## Planning Contract

### Key Technical Decisions

- KTD1. **In-place worktree directory `B/.ai-team-worktrees/<slug>` with `.git/info/exclude` registration** (session-settled: user-directed — chosen over placing `.ai-team-worktrees` in `os.path.dirname(B)` which is parent folder `A`: fulfills R2 and R3 so neither `A` nor `C/D/E` is ever touched by git or directory creation). Both `team/gitops.py:create_worktree` and `team/office.py:_unique_slug` resolve `WT_DIR` under `os.path.join(os.path.abspath(repo), WT_DIR)`. When `B` has no `.git` of its own (`git rev-parse --show-toplevel` != `os.path.realpath(B)`), `ensure_local_git_repo(B)` runs `git init`, configures `core.longpaths=true` on Windows, writes `.ai-team-worktrees/` into `B/.git/info/exclude`, stages existing files in `B`, and creates an initial commit (`git commit --allow-empty -m "ai-team: initial baseline"`).
- KTD2. **Dual-scope path guard in `team/policy.py:decide`, `--add-dir` in `team/drivers.py`, and post-turn reference integrity check in `team/checks.py`** (session-settled: user-directed — chosen over single-root `_outside_scope(p, wt)`: fulfills R2 and R4 by allowing read-only tools inside `reference_root` `A` across both ACP and `HeadlessClaude` seats while rejecting any write/edit/bash-redirection outside `wt` and verifying post-turn that no sibling file in `A` was modified).
- KTD3. **Dedicated `team/ce_skills.py` module for pre-flight CLI provisioning and Role × CE Playbook compilation in `team/personas.py:Composer`** (session-settled: user-approved — chosen over hardcoding one-line skill names in prompts: fulfills R5, R6, R7, and R8 by syncing `ce-*` skill folders to Claude, Kiro, and Antigravity AND injecting structured CE step-by-step role playbooks for `po`, `tech_lead`, `scrum`, `implementer`, `fixer`, `qa`, and `reviewer` so `GLM-5.3` and all CLI models follow identical CE standards).
- KTD4. **Unified `team/question_router.py` backed by `team/models.py:ModelRouter` (`choose` / `probability` for `Decision-1` and `write` for `GLM-5.3`)** (session-settled: user-approved — chosen over only handling system retry menus in `_delegate_question`: fulfills R9, R10, R11, and R12 by routing multiple-choice questions to `Decision-1` and open-ended descriptive questions to `GLM-5.3` with read-only reference snippets from `A`).

### High-Level Technical Design

```mermaid
flowchart TB
  subgraph Wizard["1. Dispatch Wizard & Room Creation"]
    ScanA["Scan Parent Folder A (scan_project returns subdirs)"] --> PickB["User Picks Target Subfolder B (Siblings C/D/E Marked Read-Only)"]
    PickB --> CreateRoom["POST /api/team/rooms: repo=A/B, reference_root=A, delegate=True"]
  end

  subgraph Preflight["2. RoomRunner Pre-Flight (_run)"]
    CreateRoom --> CheckCE["ensure_ce_installed(): Verify & Sync ce-* Skills for Claude / Kiro / AGY"]
    CheckCE --> CheckGit["ensure_local_git_repo(B): git init + initial commit if B has no local .git"]
    CheckGit --> CreateWT["create_worktree(B): B/.ai-team-worktrees/<slug> (excluded in B/.git/info/exclude)"]
  end

  subgraph Execution["3. Role Execution & Guardrails"]
    CreateWT --> RolePrompt["personas.Composer: Inject Role CE Playbook (po/tech_lead/scrum/implementer/fixer/qa/reviewer)"]
    RolePrompt --> PolicyGuard["policy.decide + HeadlessClaude --add-dir A + checks.py: Read B + A (C/D/E); Write Locked to B"]
    PolicyGuard --> AskCheck{"Agent Asks Question?"}
    AskCheck -->|Multiple-Choice (Options)| Dec1["ModelRouter.choose (Decision-1): Score Options vs. Goal & Spec"]
    AskCheck -->|Descriptive / Open-Ended| GLM["ModelRouter.write (GLM-5.3): Synthesize Answer from Goal + Spec + Reference Root A"]
    Dec1 --> Streak{"Streak <= 3?"}
    GLM --> Streak
    Streak -->|Yes| Continue["Feed Answer Back via ACP / --resume Next Turn"]
    Streak -->|No / Destructive| Inbox["Escalate to User Inbox"]
  end
```

---

## Implementation Units

### U1. Target Subfolder `B` Discovery, Local Git Bootstrap, In-Place Worktree, and Read-Only Reference `A` Guard

- **Goal:** Support scanning parent folder `A` for subfolders, locking all git/worktree initialization and file writes strictly to target subfolder `B` (`A/B`), and treating `A` (`C/D/E`) as a read-only reference root.
- **Requirements:** R1, R2, R3, R4 (KTD1, KTD2)
- **Dependencies:** None
- **Files:**
  - `team/scan.py`
  - `team/rooms.py`
  - `team/gitops.py`
  - `team/policy.py`
  - `team/checks.py`
  - `team/drivers.py`
  - `team/office.py`
  - `server.py`
  - `tests/team/test_gitops_subfolder.py`
- **Approach:**
  1. In `team/scan.py:scan_project`, populate `subdirs: List[str]` with sorted first-level non-hidden subdirectories of `path` (excluding `.git`, `.ai-team-worktrees`, `node_modules`, `__pycache__`, `.venv`, `venv`), and allow `scan_project` to succeed without error even when parent folder `A` is not itself a Git repo.
  2. In `team/rooms.py`, add `reference_root: str = ""` and `target_subdir: str = ""` to `Room` and `delegate: bool = True` by default; in `server.py` (`POST /api/team/rooms`, lines 465–475) and `team/office.py:create_room`, accept `target_subdir`: when `target_subdir` is provided under folder `A`, resolve `B = os.path.join(A, target_subdir)`, set `room.repo = B`, `room.reference_root = A`, and `room.target_subdir = target_subdir`.
  3. In `team/gitops.py`, add `ensure_local_git_repo(repo: str) -> bool`: check whether `repo` (`B`) has its own `.git` directory (`git rev-parse --show-toplevel` resolves to `os.path.realpath(repo)`). If not, run `git init` inside `B`, write `.ai-team-worktrees/` into `B/.git/info/exclude`, stage existing files in `B`, and create an initial baseline commit (`git commit --allow-empty -m "ai-team: initial baseline"`).
  4. In `team/gitops.py:create_worktree` and `team/office.py:_unique_slug`, place `WT_DIR` (`.ai-team-worktrees`) inside `os.path.join(os.path.abspath(repo), WT_DIR)` (`B/.ai-team-worktrees/<slug>`) instead of `os.path.dirname(repo)`, ensuring `B/.git/info/exclude` always contains `.ai-team-worktrees/` so `uncommitted()` and `changed_files()` stay clean and parent folder `A` is never touched.
  5. In `team/policy.py:decide`, accept `reference_root: str = ""` (and inspect shell output redirections `>` / `>>` / `tee` / `Out-File` / `Set-Content` / `Remove-Item` / `rm` / `mv` / `cp` in bash commands): allow read-only operations on paths inside `scope` (`wt`) OR `reference_root` (`A`), while returning `PermissionDecision(outcome="reject", ...)` for any write/edit/delete/redirection targeting a path outside `scope` (`wt`).
  6. In `team/drivers.py:seat_launch`, when `reference_root` is set on the room, pass `--add-dir <reference_root>` to `HeadlessClaude` (`claude`, `glm`, `glm_high`) so read tools can inspect sibling folders `C/D/E` in `A`, and in `team/checks.py`, snapshot sibling file mtimes/hashes under `reference_root` (excluding `B`) before and after each turn to verify zero files in `A` outside `B` were modified.
- **Patterns to follow:** `team/gitops.py:create_worktree`, `team/policy.py:decide`, `team/checks.py`
- **Test scenarios:**
  - **Happy path (B has no git):** Given parent folder `A` containing subfolders `B`, `C`, `D` where neither `A` nor `B` has `.git`, `scan_project(A)` returns `subdirs=["B", "C", "D"]`, and calling `ensure_local_git_repo(B)` + `create_worktree(B, "task")` initializes `.git` inside `B`, creates the worktree at `B/.ai-team-worktrees/B-task`, leaves `A`, `C`, and `D` completely untouched, and reports clean `uncommitted(wt)` and `changed_files(wt)`.
  - **Edge case (A is a git repo but B is selected as independent target):** Given `A` has `.git` and `B` does not have `.git`, `ensure_local_git_repo(B)` detects `git rev-parse --show-toplevel` is `A` (not `B`), initializes an isolated local `.git` inside `B` itself, and creates the worktree inside `B` without modifying `A`'s git state.
  - **Permission guard (read C/D allowed, write C/D rejected):** Calling `policy.decide` for a read tool on `D:/A/C/ref.py` with `scope=[wt.path]` and `reference_root="D:/A"` returns `outcome == "allow"`, whereas a write tool on `D:/A/C/ref.py` or a bash command `echo x > ../C/ref.py` returns `outcome == "reject"`.
- **Verification:** `python -m pytest tests/team/test_gitops_subfolder.py -q` passes with all subfolder git, worktree, and read/write isolation scenarios green.

---

### U2. Pre-Work `compound-engineering` Provisioning & Cross-CLI/Model Role Playbooks

- **Goal:** Verify and auto-provision `compound-engineering` before any room starts work, and configure explicit role-level CE capabilities across all CLIs (`claude`, `kiro`, `agy`) and all models (including `GLM-5.3` seats `glm`/`glm_high`).
- **Requirements:** R5, R6, R7, R8 (KTD3)
- **Dependencies:** U1
- **Files:**
  - `team/ce_skills.py`
  - `team/personas.py`
  - `team/prompt_defaults.py`
  - `team/drivers.py`
  - `team/runner.py`
  - `config.py`
  - `tests/team/test_ce_skills.py`
- **Approach:**
  1. Create `team/ce_skills.py` with:
     - `REQUIRED_CE_SKILLS = ("ce-brainstorm", "ce-strategy", "ce-plan", "ce-doc-review", "ce-work", "ce-debug", "ce-simplify-code", "ce-test-browser", "ce-code-review", "ce-compound")`
     - `ROLE_CE_MAP`: mapping each room role (`po`, `tech_lead`, `scrum`, `implementer`, `fixer`, `qa`, `reviewer`) to its exact CE skills and structured execution checklist.
     - `ensure_ce_installed(cfg, cli_map=None) -> Dict[str, Any]`:
       - Discovers the canonical local `ce-*` skills directory (checking `~/.gemini/config/skills`, `cfg.team_plugin_dirs`, and `<repo>/.compound-engineering/skills`).
       - If missing from a target CLI's skill/plugin location (`~/.gemini/config/skills/ce-*` for `agy`, `~/.kiro/skills/ce-*` for `kiro`, and a synced plugin/skills directory for `claude`/`glm`/`glm_high` `--plugin-dir`), automatically copies/syncs the `ce-*` skill folders from the local canonical source; if no local source exists on the machine, runs fallback provisioning so every CLI has the `ce-*` skills installed before room execution begins.
     - `role_ce_directive(role: str, notes_dir: str, reference_root: str = "", target_subdir: str = "") -> str`:
       - Returns the explicit CE capability block for `role` (including the bound `ce-*` skill names, step-by-step CE methodology for that role, read-only reference instructions for `reference_root` `A` if set, and hard guardrails directing plans/learnings to `{notes_dir}` and forbidding unauthorized `git push` / PRs).
  2. In `team/drivers.py:seat_launch`, automatically include the provisioned CE plugin directory in `plugin_dirs` for all `HeadlessClaude` seats (`claude`, `glm`, `glm_high`).
  3. In `team/personas.py:Composer`, `team/prompt_defaults.py`, and `team/runner.py:RoomRunner._run`, run `ensure_ce_installed` at the start of `_run()` (logging a timeline event `"已確認 compound-engineering 技能就緒（Claude / Kiro / AGY / GLM-5.3 全角色已掛載）"`), and inject `role_ce_directive(...)` into every role prompt (`po`, `tech_lead`, `scrum`, `implementer`, `fixer`, `qa`, `reviewer`).
- **Patterns to follow:** `team/personas.py:Composer`, `team/prompt_defaults.py`, `team/drivers.py:seat_launch`
- **Test scenarios:**
  - **Pre-flight check & auto-sync:** When a target CLI skill directory is missing `ce-plan` or `ce-code-review`, `ensure_ce_installed` syncs the missing `ce-*` skill directories from the local canonical source and returns `ok=True` with synced skill paths.
  - **All 7 roles explicitly mapped:** For every role in `("po", "tech_lead", "scrum", "implementer", "fixer", "qa", "reviewer")`, `role_ce_directive(role, notes_dir="/tmp/notes", reference_root="/tmp/A", target_subdir="B")` includes the role's designated `ce-*` skills, the `{notes_dir}` output rule, and the read-only `A` vs. writable `B` boundary.
  - **Cross-CLI and `GLM-5.3` parity:** `Composer` injects the role-specific CE playbook regardless of whether the assigned seat is `claude`, `kiro`, `agy`, `glm`, or `glm_high`.
- **Verification:** `python -m pytest tests/team/test_ce_skills.py -q` passes.

---

### U3. Question Router: `Decision-1` for Multiple-Choice Questions & `GLM-5.3` for Descriptive Questions

- **Goal:** Automatically classify and route agent questions during room execution — sending multiple-choice questions to `Decision-1` (`ModelRouter.choose`) and open-ended descriptive questions to `GLM-5.3` (`ModelRouter.write`) — with a 3-turn streak safeguard before escalating to the human inbox.
- **Requirements:** R9, R10, R11, R12 (KTD4)
- **Dependencies:** U1, U2
- **Files:**
  - `team/question_router.py`
  - `team/runner.py`
  - `team/drivers.py`
  - `tests/team/test_question_router.py`
- **Approach:**
  1. Create `team/question_router.py` with:
     - `classify_question(title: str, detail: str, options: Optional[List[Dict[str, str]]] = None) -> Tuple[str, List[Dict[str, str]]]`:
       - Detects whether a question is `"multiple_choice"` (explicit `options` list with >= 2 choices, or structured bulleted/lettered choices `A) ... B) ...` / `1. ... 2. ...` extracted from `detail`) vs. `"descriptive"` (open-ended question or `NEEDS_CONTEXT` request without discrete options).
     - `route_question(router: ModelRouter, *, goal: str, spec_text: str, plan_text: str, reference_root: str, worktree: str, title: str, detail: str, options: Optional[List[Dict[str, str]]] = None, room_id: str = "", stage: str = "") -> Dict[str, Any]`:
       - **Multiple-choice branch (`Decision-1`)**: Calls `router.choose` / `router.probability` in `team/models.py` (evaluating each option against `goal` and `spec_text` so `Decision-1` selects the highest-probability option least likely to deviate from the development goal) and returns `{"kind": "multiple_choice", "by": "Decision-1", "option_id": chosen_id, "answer_text": chosen_label, "reason": reason}`.
       - **Descriptive branch (`GLM-5.3`)**: Gathers relevant read-only context snippets from `spec_text`, `plan_text`, and `reference_root` (`A`) if referenced filenames/symbols appear in the question, then calls `router.write` (`GLM-5.3`) in `team/models.py` to produce a concise, authoritative answer aligned with `goal`, returning `{"kind": "descriptive", "by": "GLM-5.3", "option_id": "", "answer_text": reply, "reason": "依開發目標與唯讀參考資料解答"}`.
  2. In `team/drivers.py` (ACP `session/request_permission` handling), when an ACP request carries question options, invoke the room's question callback to auto-select via `route_question` when within the streak cap.
  3. In `team/runner.py`:
     - Update `_delegate_question` and `_ask` so inbox questions with options use `Decision-1` (`route_question` multiple-choice branch) and descriptive questions use `GLM-5.3` (`route_question` descriptive branch), tracking `delegate_streak` per stage (escalating to human inbox when `streak > 3` or when the question involves destructive operations).
     - When an agent turn (`HeadlessClaude` or `AcpDriver`) returns `status == "NEEDS_CONTEXT"` or emits a blocking question in `summary`/`blockers`, automatically invoke `route_question`, log the `Decision-1` or `GLM-5.3` answer in the room event stream, and pass the answer into the next `--resume` / follow-up turn via `pending_fix` without stopping the room (unless `streak > 3`).
- **Patterns to follow:** `team/models.py:ModelRouter`, `team/runner.py:_delegate_question`
- **Test scenarios:**
  - **Multiple-choice routing to `Decision-1`:** Given a question with 3 architectural options (`A`, `B`, `C`) and a room goal, `route_question` classifies it as `"multiple_choice"`, calls `ModelRouter.choose`, and returns the option least likely to deviate from the goal (`by="Decision-1"`).
  - **Descriptive routing to `GLM-5.3` with reference context:** Given an open-ended `NEEDS_CONTEXT` question asking how a sibling module in `A/C` formats payloads, `route_question` classifies it as `"descriptive"`, reads the referenced snippet from `reference_root`, calls `ModelRouter.write`, and returns a descriptive answer (`by="GLM-5.3"`).
  - **Streak cap escalation:** When the same stage asks 4 consecutive questions without completing the stage, the 4th question returns `handoff=True` and escalates to the user's Inbox (`waiting_user`).
- **Verification:** `python -m pytest tests/team/test_question_router.py -q` passes.

---

### U4. Frontend Subfolder `B` Selector (`DispatchWizard`) & Room Scope / CE Status Display (`SessionPanel`)

- **Goal:** Let users select target subfolder `B` (while marking sibling folders `C/D/E` as read-only reference sources) in the Dispatch Wizard, and show the active subfolder scope, CE role status, and question routing status in `SessionPanel`.
- **Requirements:** R1, R2, R6, R9
- **Dependencies:** U1, U2, U3
- **Files:**
  - `pixeloffice/lib/types.ts`
  - `pixeloffice/lib/api.ts`
  - `pixeloffice/components/DispatchWizard.tsx`
  - `pixeloffice/components/SessionPanel.tsx`
  - `tests/test_pixel_ui.py`
- **Approach:**
  1. Update `pixeloffice/lib/types.ts` (`TeamRoom`, `TeamScanResult` adding `subdirs?: string[]`) and `pixeloffice/lib/api.ts` (`createRoom` payload) to include `target_subdir?: string` and `reference_root?: string`.
  2. In `pixeloffice/components/DispatchWizard.tsx`:
     - Automatically trigger `teamApi.scan(repo)` when `repo` changes or when the user clicks `"掃描"`.
     - When `scan?.subdirs` is non-empty, render a **"工作目標子資料夾（B）選擇器"**:
       - Option `"（整個資料夾皆可修改）"` plus a selectable chip/button for each subfolder in `scan.subdirs`.
       - When a subfolder `B` is selected (`targetSubdir`), highlight `B` in emerald as **"目標工作區（可修改，無 Git 時自動於 B 建立本地 Git 與 Worktree）"** and display all other sibling folders (`C, D, E...`) with a badge **"唯讀參考資料來源（不修改、不碰 Git）"**.
     - Pass `target_subdir: targetSubdir` in `teamApi.createRoom(...)`.
  3. In `pixeloffice/components/SessionPanel.tsx`:
     - In the room overview / header area, display the active scope: if `room.target_subdir` or `room.reference_root` is set, show **"目標修改資料夾：`B` ｜ 唯讀參考母目錄：`A`"**; also show the active governance badge **"CE 全角色技能已掛載 ｜ 選項題 → Decision-1 ／ 描述題 → GLM-5.3"**.
  4. Rebuild the static export (`cd pixeloffice && npm test && npm run build`) and add a UI verification check in `tests/test_pixel_ui.py`.
- **Patterns to follow:** `pixeloffice/components/DispatchWizard.tsx`, `pixeloffice/components/SessionPanel.tsx`
- **Test scenarios:**
  - **Subfolder selector in DispatchWizard:** Scanning a parent directory `A` with subfolders `["app-b", "ref-c", "ref-d"]` renders the subfolder selector; clicking `"app-b"` marks `"app-b"` as the writable target and `"ref-c"`, `"ref-d"` as read-only reference folders, and submits `target_subdir: "app-b"`.
  - **SessionPanel scope & CE routing badge:** Selecting a room with `target_subdir="app-b"` and `reference_root="D:/A"` renders both the writable target vs. read-only reference indicator and the CE + `Decision-1`/`GLM-5.3` routing badge.
- **Verification:** `cd pixeloffice && npm test && npm run build` and `python -m pytest tests/test_pixel_ui.py -q` pass.

---

## Verification Contract

| Gate | Command | Applies To | Pass Criterion |
|---|---|---|---|
| Subfolder Git & Policy Unit Tests | `python -m pytest tests/team/test_gitops_subfolder.py -q` | U1 | All tests pass; parent folder `A` and siblings `C/D/E` remain untouched when `B` initializes local git and worktree |
| CE Provisioning & Role Playbook Tests | `python -m pytest tests/team/test_ce_skills.py -q` | U2 | All 7 roles receive explicit CE skill directives across Claude, Kiro, AGY, and `GLM-5.3` |
| Question Router Tests | `python -m pytest tests/team/test_question_router.py -q` | U3 | Multiple-choice questions route to `Decision-1`; descriptive questions route to `GLM-5.3`; streak > 3 escalates to inbox |
| Existing Team & Server Suite | `python -m pytest tests/team/test_modelprefs.py tests/test_server_team.py -q` | U1, U2, U3 | Zero regressions across existing team and server API tests |
| Frontend Unit & Static Build | `cd pixeloffice && npm test && npm run build` | U4 | `tsx --test` unit tests pass and Next.js static export updates `web/pixel` cleanly |
| Playwright E2E Verification | `python -m pytest tests/test_pixel_ui.py -q` | U4 | Headless Chromium E2E test passes |

---

## Definition of Done

- All 4 Implementation Units (`U1`–`U4`) are implemented and verified against `R1`–`R12`.
- Opening parent folder `A` and choosing subfolder `B` initializes local git and `.ai-team-worktrees` inside `B` itself if needed, never touching `A` or `C/D/E`, while allowing agents to read reference files from `A` (`C/D/E`) and blocking any writes outside `B`.
- Every room verifies and syncs `compound-engineering` before starting work and injects explicit role-level CE playbooks (`po`, `tech_lead`, `scrum`, `implementer`, `fixer`, `qa`, `reviewer`) across all CLIs and `GLM-5.3`.
- Mid-execution and turn-end questions are automatically classified and routed (`Decision-1` for multiple-choice, `GLM-5.3` for descriptive) with a 3-turn streak safeguard.
- Static frontend bundle in `web/pixel` is rebuilt and all verification gates pass with zero dead-end or experimental code left in the diff.
