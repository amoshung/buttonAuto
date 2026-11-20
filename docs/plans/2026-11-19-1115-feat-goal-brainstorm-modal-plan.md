---
title: "feat: Goal Brainstorm Modal with Multi-CLI Fallback & Plan Generation"
type: feat
status: ready
date: 2026-11-19
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-brainstorm
execution: code
---

# feat: Goal Brainstorm Modal with Multi-CLI Fallback & Plan Generation

## Goal Capsule

- **Objective:** 在房間「目標」區塊提供彈出式 Brainstorm 討論視窗，預設使用 `glm-5.3`（一般思考／medium）並支援無 `glm` 時自動降級／手動切換其他可用 CLI 與模型，讓使用者透過多輪對話收斂粗略想法，並在任何時候「生成計畫」、預覽確認後寫入 `docs/plans/` 並自動套用至房間目標與進度追蹤。
- **Scope:** 前端 `GoalBrainstormModal` 元件與 `SessionPanel` 整合、前端 `teamApi`／型別擴充、後端 Brainstorm 單輪對話與計畫生成／寫入 API、以及前後端單元測試。
- **Success Criteria:**
  - 不佔用右邊欄（`480px–580px`）空間，從目標區按鈕開啟置中 Modal，關閉後保留該房間討論紀錄。
  - 有 `glm` 時預設 `glm`（`glm-5.3` 一般思考）；無 `glm` 時自動降級至第一個可用的 CLI（`claude` / `kiro` / `agy`）與模型，且可隨時切換。
  - 任何輪次皆可觸發「生成計畫」，在 Modal 內預覽／微調 Markdown 後確認寫入 `docs/plans/`，並自動呼叫 `sync_room_plan(..., apply_goal=True)` 更新房間目標與 `計畫進度`。

## Product Contract (Requirements)

### R1. 介面配置與觸發入口（不擠壓右邊欄）
- 在 `pixeloffice/components/SessionPanel.tsx`「目標（由你自行寫入或跟隨 CE 計畫）」按鈕列新增 **Brainstorm** 觸發按鈕（例如 `✨ Brainstorm`）。
- 點擊後開啟**置中彈出式 Brainstorm Modal**（桌機寬度約 `800px–960px`），避免佔用原本 `480px–580px` 的右邊欄空間，也避免與右邊欄底部的派工指示輸入框混淆。
- Modal 關閉後保留當前房間的討論紀錄（於前端／`localStorage` 依 `room.id` 保存），誤關或想繼續討論時不必重來。

### R2. 模型預設與多 CLI 降級切換
- Modal 頂部提供「CLI 座位與模型」選擇器，沿用 `filterCallableSeatChoices` 與 `seatModelOptions` 偵測清單（`glm`、`glm_high`、`claude`、`kiro`、`agy`）。
- **預設邏輯**：
  - 若環境可使用 `glm`（`glm-5.3` 一般思考／medium），預設選用 `glm`。
  - 若使用者未設定或無 `glm` 可用，**自動降級**選用第一個可用的 CLI 與其預設／已偵測模型，不得因缺少 GLM-5.3 而阻擋使用。
- 討論過程中允許使用者隨時切換 CLI 座位或模型繼續對話。

### R3. 多輪 Brainstorm 對話體驗
- 開啟 Modal 時，自動帶入目前目標欄位中的粗略文字（若有）以及房間綁定的工作資料夾脈絡作為討論起點。
- 使用者可與 AI 進行多輪文字問答；AI 以收斂需求、釐清範圍、指出盲點與提出選項為主。
- 對話區常駐 **「生成計畫」** 按鈕，使用者在任何輪次皆可主動觸發。

### R4. 生成計畫、預覽確認與 CE 流程銜接
- 點擊「生成計畫」後，由當前選用的 CLI／模型將對話收斂成符合 `team/ce_sync.py` 解析規範的 Markdown 計畫草稿：
  - 檔名格式：`docs/plans/YYYY-MM-DD-HHMM-<slug>-plan.md`
  - 內容結構：YAML frontmatter、`- **Objective:** ...`、`## Scope`、`## Implementation Units`（含 `- [ ]` 任務清單，讓房間 `計畫進度 %` 直接解析）。
- **預覽與確認**：生成的計畫先在 Modal 內顯示預覽（支援直接編輯檔名 slug 與 Markdown 內文），使用者確認「寫入並套用」後才實際寫檔。
- **寫入後連動**：寫入 `<repo>/docs/plans/` 後自動切換該房間的 CE 計畫並將總目標帶入房間「目標」欄位。

## High-Level Technical Design

```text
[SessionPanel.tsx] ──(點 ✨ Brainstorm)──> [GoalBrainstormModal.tsx]
                                                │
       ┌────────────────────────────────────────┴────────────────────────────────────────┐
       ▼ (1. 對話 / 生成草稿)                                                            ▼ (2. 預覽確認寫入)
POST /api/team/rooms/brainstorm                                                 POST /api/team/rooms/brainstorm/save_plan
  │                                                                               │
  ├─ seat in ("glm", "glm_high") & ModelRouter 可用 ──> ModelRouter.write()       ├─ 安全路徑檢查 (<repo>/docs/plans/*.md)
  │                                                                               ├─ 寫入 .md 檔 (防覆蓋碰撞)
  └─ 其他 CLI ("claude" | "kiro" | "agy") ───────────> seat_launch + make_driver  └─ office.sync_room_plan(room_id, plan_rel, apply_goal=True)
                                                       (read_only=True)
```

### Key Technical Decisions
1. **GLM 與其他 CLI 的統一後端派發策略（per R2, R3）**：
   - 當 `seat` 為 `glm`／`glm_high` 且 `ModelRouter` 已設定時，優先使用 `ModelRouter.write(prompt, effort=...)`（`glm` 用 `"normal"`、`glm_high` 用 `"high"`），省去啟動 `claude` 子程序延遲且不依賴本機 `claude` CLI。
   - 若 `seat` 為 `claude`／`kiro`／`agy`（或 `glm` 改走 CLI），複用 `team/drivers.py` 的 `seat_launch` + `make_driver(..., read_only=True)` 在該房間工作目錄下執行單次 `driver.prompt(...)` 並確保 `finally: driver.close()`。
   - 若房間為 `demo=True`（示範房間），直接回傳結構化示範回覆／示範計畫草稿，不耗用真實 API／CLI 額度。
2. **兩段式生成與寫入（per R4）**：
   - `mode: "chat" | "generate_plan"` 共用 `POST /api/team/rooms/brainstorm`：前者回傳助理回覆文字，後者回傳建議 `filename` 與完整 Markdown `content` 供前端預覽編輯，**不觸碰磁碟**。
   - 獨立的 `POST /api/team/rooms/brainstorm/save_plan` 驗證 `filename` 僅包含安全字元並落於 `<repo>/docs/plans/` 內，寫入後呼叫既有 `office.sync_room_plan(room_id, plan_rel=..., apply_goal=True)` 回傳最新房間狀態與 `ce` 資訊。
3. **前端預設座位與降級選擇（per R2）**：
   - 複用 `pixeloffice/components/SessionPanel.tsx` 已匯出的 `filterCallableSeatChoices` 與 `seatModelOptions`：若可呼叫清單包含 `'glm'`，預設選 `'glm'`（model `'glm-5.3'`）；否則自動選清單第一個非 `'auto'` 的可用座位及其第一個可用模型。

## Implementation Units

### U1: 後端 Brainstorm 對話與計畫草稿生成邏輯 (`team/office.py`) — per R2, R3, R4
- [ ] 在 `team/office.py` 新增 `brainstorm_room(room_id, messages, mode="chat", seat="", model="", goal_draft="") -> Dict[str, Any]`：
  - 驗證房間存在與 `messages` 格式（每筆含 `role` / `content`）。
  - 依 `mode` 組裝系統提示詞：
    - `chat`：引導收斂需求、釐清範圍與風險（繁體中文、精簡回答）。
    - `generate_plan`：要求輸出含 YAML frontmatter、`- **Objective:** ...`、`## Scope`、`## Implementation Units`（含 `- [ ]` 任務項目）的完整 Markdown，並萃取／產生 `YYYY-MM-DD-HHMM-<slug>-plan.md` 建議檔名。
  - 若 `room.demo` 為真，回傳對應示範文字或示範計畫結構。
  - 依 `seat` 分流：`glm`／`glm_high` 優先走 `self.model_router.write(...)`；其餘（或無 `model_router` 時）走 `seat_launch` + `make_driver(..., read_only=True)`，捕捉錯誤並轉為友善 `ValueError`／`TeamError`。
- [ ] 在 `team/office.py` 新增 `save_brainstorm_plan(room_id, filename, content, apply_goal=True) -> Dict[str, Any]`：
  - 驗證 `room.repo` 存在、`filename` 為安全的 `.md` 檔名（禁止 `..`、斜線、非法字元），目標路徑嚴格限定於 `<repo>/docs/plans/`（必要時建立目錄；若同名已存在則自動加 `-2` 等後綴避免靜默覆寫）。
  - 寫入 UTF-8 檔案後呼叫 `self.sync_room_plan(room_id, plan_rel=..., apply_goal=apply_goal)`，回傳更新後的 `room`、`ce` 與 `plan_rel`。

### U2: 後端 HTTP API 端點 (`server.py`) — per R2, R3, R4
- [ ] 在 `server.py` `do_POST` 新增：
  - `POST /api/team/rooms/brainstorm`：解析 `id`、`messages`、`mode`、`seat`、`model`、`goal_draft`，呼叫 `t.brainstorm_room(...)`。
  - `POST /api/team/rooms/brainstorm/save_plan`：解析 `id`、`filename`、`content`、`apply_goal`，呼叫 `t.save_brainstorm_plan(...)`。
- [ ] 錯誤處理與現有 `/api/team/rooms/*` 一致（缺欄位回 400 `TeamError`，執行錯誤回 `{ok: false, text: ...}`）。

### U3: 前端 API、型別與 `GoalBrainstormModal` 元件 (`pixeloffice/lib/types.ts`, `pixeloffice/lib/api.ts`, `pixeloffice/components/GoalBrainstormModal.tsx`) — per R1, R2, R3, R4
- [ ] 在 `pixeloffice/lib/types.ts` 定義 `BrainstormMessage`、`BrainstormResponse`、`SaveBrainstormPlanResponse` 型別。
- [ ] 在 `pixeloffice/lib/api.ts` `teamApi` 新增 `brainstormRoom(...)` 與 `saveBrainstormPlan(...)`。
- [ ] 建立 `pixeloffice/components/GoalBrainstormModal.tsx`：
  - 沿用 `DispatchWizard.tsx` Modal 遮罩與鍵盤 `Escape` 關閉樣式（桌機 `max-w-4xl`）。
  - 頂部工具列：CLI 座位下拉選單 + 模型下拉選單（複用 `filterCallableSeatChoices` 與 `seatModelOptions`，有 `glm` 預設 `glm`／`glm-5.3`，無 `glm` 自動降級第一個可用 CLI／模型）、`清空對話` 按鈕。
  - 主區域雙模式視圖：
    1. **討論模式**：顯示多輪對話氣泡、輸入框、`送出` 按鈕與常駐 `生成計畫` 按鈕；開啟時若對話為空且有 `goalDraft`，顯示已帶入的粗略目標提示。
    2. **計畫預覽模式**：點擊 `生成計畫` 後進入預覽編輯，可修改檔名與 Markdown 內容，提供 `返回繼續討論` 與 `確認寫入並套用` 按鈕。
  - 依 `room.id` 在元件外／`localStorage` 保留該房間對話紀錄，關閉 Modal 不遺失。

### U4: `SessionPanel` 整合與測試 (`pixeloffice/components/SessionPanel.tsx`, `tests/`) — per R1, R2, R4
- [ ] 在 `pixeloffice/components/SessionPanel.tsx` 目標區塊按鈕列加入 `✨ Brainstorm` 按鈕，點擊開啟 `GoalBrainstormModal`；確認寫入計畫後觸發 `onRoomUpdated()` 並同步更新 `goalDraft` 與 `selectedPlanRel`，顯示 Toast 通知。
- [ ] 補齊測試與前端建置驗證：
  - `tests/team/test_brainstorm.py`：測試 `brainstorm_room`（`demo` 模式、`glm` 走 `ModelRouter`、非 `glm` CLI 降級呼叫 `make_driver`）、`save_brainstorm_plan`（路徑穿越防護、寫檔後同步 `criteria` 與 `goal`）。
  - `tests/test_server_team.py`：測試 `/api/team/rooms/brainstorm` 與 `/api/team/rooms/brainstorm/save_plan` HTTP 端點。
  - 執行 `npm run build`（於 `pixeloffice/`）與 `pytest` 確認無型別／建置／回歸錯誤。

## Verification Contract

- **Backend unit & API tests:** `python -m pytest tests/team/test_brainstorm.py tests/test_server_team.py -q`
- **Frontend typecheck & static export:** `cd pixeloffice && npm run build`
- **Test Scenarios:**
  1. `glm` 可用時，`GoalBrainstormModal` 預設選中 `glm`（`glm-5.3`）；當 `modelsView` 不含 `glm`（或 `glm` 不可呼叫）時，自動降級選中第一個可用 CLI（如 `claude`／`kiro`／`agy`）及其模型。
  2. 送出多輪對話訊息至 `/api/team/rooms/brainstorm`（`mode="chat"`），後端正確組裝歷史訊息並回傳助理回覆。
  3. 觸發 `mode="generate_plan"` 回傳含 `- **Objective:**` 與 `- [ ]` 項目的 Markdown 草稿與建議檔名，且尚未寫入磁碟。
  4. 呼叫 `/api/team/rooms/brainstorm/save_plan` 寫入 `<repo>/docs/plans/*.md`（含拒絕 `../` 路徑穿越），並自動透過 `sync_room_plan(..., apply_goal=True)` 更新房間 `goal` 與 `criteria`。

## Definition of Done

- [ ] `team/office.py` 與 `server.py` 完成 Brainstorm 對話、計畫生成與安全寫入端點。
- [ ] `pixeloffice/components/GoalBrainstormModal.tsx` 與 `SessionPanel.tsx` 完成整合，並支援無 GLM-5.3 時自動降級與手動切換 CLI／模型。
- [ ] 新增與既有相關測試通過，且 `pixeloffice` `npm run build` 成功無錯誤。
