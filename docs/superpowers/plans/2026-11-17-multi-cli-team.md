# AI 工程助理：多 CLI 敏捷團隊（中鋼主權 AI 扮演角色，指揮 agy／Claude／Kiro CLI 閉環開發）

> 狀態：已被 `docs/superpowers/specs/2026-11-17-multi-cli-team-design.md` 取代（2026-11-17）。保留作為討論紀錄。
> 依據：recon\20261117-074209、recon\20261117-081433、recon\acp-20261117-081449.json、recon\model-bench-20261117-081814.md

## 1. 目標
像素辦公室指揮多種 AI 編碼 CLI，由中鋼主權 AI 扮演敏捷團隊角色，沿著每個專案的目標推進到「已驗證完成」，**盡量不需要使用者處理**，而且**每一關都有代理改不了的證據**。

### 不做
- 不操作任何 IDE 視窗（不截圖、不 UIA、不搶焦點）。舊的視窗驅動保留為「舊對話接手」模式，預設關閉。
- 不替使用者做產品方向的取捨；只有真正的取捨與高風險動作交給使用者。

## 2. 偵察結論
| 項目 | 結果 | 影響 |
|---|---|---|
| Antigravity CLI `agy` 1.5.9 | `%LOCALAPPDATA%\agy\bin\agy.exe`；`agy acp` 交握成功（protocolVersion 1；可 resume／load／list session；提示可帶圖片） | 主力驅動：每個工具呼叫與權限請求都以 JSON-RPC 送到助理 |
| Claude Code CLI 2.1.326 | `~\.local\bin\claude.exe`；有 headless `-p` | 第二驅動：`-p --output-format stream-json`，`--resume` 接續 |
| Kiro CLI | **找不到**（PATH、常見位置都沒有） | 待使用者提供路徑；確認後走 `kiro-cli acp`，否則走 headless |
| Kiro／Antigravity agent 視窗 | 標題 `Agent Focus`／程式 `Antigravity.exe`，UIA 讀得到 | 只用於「舊對話接手」 |
| 模型 | gemma-4-31b：1～3 秒；glm-5.3：50～80 秒（每次先思考 3.5k～7k 字），判斷較準、拉回目標的訊息具體得多（見 model-bench） | 分層使用，見 §6 |

## 3. 協同硬規則（使用者規範，程式強制）
1. 一律非互動：`agy -p … --mode plan|accept-edits --output-format text`、`claude -p …`、`kiro-cli chat --no-interactive …`；長對話優先 ACP。
2. 助理是 Orchestrator；**同一時間禁止兩個代理改同一個檔案** → 每個委派任務一個 git worktree＋功能分支，派工時宣告「可修改路徑」，完成後檢查 diff 不越界才合併。
3. 第二意見／Code Review／除錯輔助 → 另一個 CLI 以唯讀模式（`--mode plan`、`--trust-tools read,grep`、Claude 的唯讀權限）。
4. 交接 prompt 必含：專案背景、目標檔案路徑、限制、預期輸出格式。

## 4. 架構
```
像素辦公室／主控台（web）
        │ REST + SSE
server.py ── Orchestrator（排程、角色派工、關卡判定）
        ├─ Driver 介面：start / prompt / on_event / answer_permission / cancel / resume
        │    ├─ AcpDriver（agy acp；kiro-cli acp 若可用）
        │    ├─ HeadlessDriver（claude -p stream-json；kiro-cli --no-interactive；agy -p）
        │    └─ WindowDriver（現有 automation.py，舊對話接手，預設關）
        ├─ Personas（提示詞＋允許的動作；規則派工，不讓角色互相聊天）
        ├─ Verifier（本機跑測試、git 查核、diff 邊界、故意弄壞）
        └─ Store（sessions/<seat>/ state.json、history.jsonl、progress.md、evidence/）
```
**座位（seat）**＝一個 CLI 在一個專案的一條工作線：`cli`、`project_path`、`worktree`、`branch`、`allowed_paths`、`session_id`、`goal`、`phase`、`persona_on_duty`。

## 5. 角色與關卡
| 關卡 | 角色 | 過關證據（代理改不了的） |
|---|---|---|
| ① 目標＋驗收條件 | PO | 每條條件含可執行的檢查指令；主權 AI 審查具體度；使用者確認（「積極」自主程度可自動採用） |
| ② 計畫 | Tech Lead | 計畫逐條對應驗收條件（程式檢查對應表無遺漏）；委派範圍寫成 allowed_paths |
| ③ 實作 | Scrum Master | 每步在功能分支 commit；git 確認存在、diff 不越界、無 TODO／pass／被略過的測試 |
| ④ 測試 | QA | **助理自己在 worktree 跑測試**看結束碼與通過數；故意弄壞一處測試必須失敗 |
| ⑤ 審查 | Reviewer＋另一個 CLI 唯讀 | 主權 AI 對照計畫審 diff；第二個 CLI 唯讀審查，意見彙整 |
| ⑥ 完成 | 全部 | ①～⑤ 全綠才合併、標記達成，證據存 evidence/ |

**對齊對話**：代理工作中只觀察並累積偏離訊號（改計畫外檔案、重複失敗指令、做範圍外功能、久未 commit）；代理停下來或訊號累積時，由對應角色開口，訊息署名（例：`【QA】`）並附目標、進度、上一關查核結果。

**權限請求（ACP）**：危險指令規則 → 唯讀操作自動允許 → Tech Lead 評估風險（灰色地帶找第二意見）→ 高風險才交給使用者。headless 模式無法逐一攔截，安全靠 worktree 隔離＋事後 diff 查核。

## 6. 模型分工
| 用途 | 模型 | 理由 |
|---|---|---|
| 快速分類、事件摘要、例行判斷 | gemma-4-31b | 1～3 秒；CLI 事件多半是結構化資料，不需要深思 |
| 角色思考：計畫審查、對齊判斷、diff 審查、灰色地帶風險 | glm-5.3（**待確認資料不出廠**） | 判斷明顯較準；只在代理停下來時呼叫，50～80 秒可接受；max_tokens 至少 8000 以免思考吃光回答 |
| 備援 | gemma-4-31b | LiteLLM 不可用或未獲准時自動退回 |

## 7. 分期與驗收
| 期 | 內容 | 驗收 |
|---|---|---|
| M1 | Driver 介面；AcpDriver（agy）；HeadlessDriver（claude）；座位模型；主控台「新增座位」 | 在 sandbox 專案用 agy 與 claude 各完成一個小任務；權限請求出現在主控台並可由規則自動回答；單元測試用假 ACP server |
| M2 | Personas＋關卡①～④；Verifier（worktree 測試、git、diff 邊界、故意弄壞） | 故意讓代理「說完成但測試失敗」→ QA 擋下並回報具體缺口；越界修改被擋 |
| M3 | 交叉審查（另一 CLI 唯讀）；平行座位；模型分層 | 兩個座位同時改不同路徑互不干擾；審查意見彙整進 evidence |
| M4 | 像素辦公室：座位＝CLI 工作線（名牌標 CLI）、角色走動、需要你收件匣 | 主控台與像素辦公室能新增座位、看關卡進度、處理待決事項 |

## 8. 待決事項（需要使用者回答）
1. **Kiro CLI 路徑**：PowerShell 執行 `(Get-Command kiro-cli).Source`。
2. **glm-5.3 是否內部部署**：其權重未公開，LiteLLM 可能轉送到智譜雲端；若會出廠，角色思考改用 gemma-4-31b。
3. **agy／claude 是否已登入**：ACP 交握不代表已登入；M1 第一個任務會順便確認。
4. **第一個試跑專案**：建議 `_AI_Projs` 下的沙盒資料夾，避免影響正式專案。
