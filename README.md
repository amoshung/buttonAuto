# 🤖 AI 工程助理
一間給 AI 代理用的**辦公室**：每個專案一間房，一位代理負責實作、另一家的代理唯讀審查。它們在獨立的 git worktree 裡依序走完「目標 → 驗收條件 → 實作與測試 → 審查」，需要你決定的事（允許指令、確認驗收條件、合併）集中在收件匣，一鍵回答。任何情況都不會自動合併。

支援的代理（座位）：Kiro、Antigravity（ACP）、Claude（headless）、GLM 一般與高思考（glm-5.3）。權限灰色地帶由 Decision-1 與 glm-5.3 判斷，設定在 `litellm.local.json`（`base_url`、`key`、`decision1_key`；不會出現在網頁或日誌）。

## 🚀 啟動
雙擊 **`restart.bat`**：
1. 關掉舊的助理。
2. 缺套件時自動安裝。
3. 在最小化的「AI Assistant Backend」視窗啟動後端（`server.py`）。
4. 等後端就緒後，自動在預設瀏覽器打開主控台 `http://127.0.0.1:8765/`。

改了程式、或主控台顯示「磁碟上的程式比執行中的新」時，再執行一次 `restart.bat`（或按主控台上的「重新啟動後端套用」）。關掉「AI Assistant Backend」視窗就會停止助理。

## 🖥 主控台
| 頁面／抽屜 | 用途 |
|---|---|
| 辦公室（預設） | 像素辦公室，一頁 2×2 間房。看得到誰在打字、誰舉手等你、誰在排隊打瞌睡；房間下方一行是狀態與正在做的事。右側「等你」可以直接回答（允許一次、開工、合併…）。點房間打開詳情：階段、驗收條件、陣容與對話用量、時間軸、對代理說話、暫停／繼續／封存。空房間「＋ 派工」開派工精靈；沒有房間時可以「試玩示範」（假代理在沙盒 repo 跑完整流程）。←／→ 或左右滑動換頁 |
| 收件匣（辦公室右上的抽屜） | 所有房間等你決定的事：權限（顯示指令）、驗收條件（開工／寫修改意見）、合併（commit 清單與改動統計）、問題與錯誤 |
| 座位（辦公室右上的抽屜） | 每種代理是否可用、額度已滿時倒數到下次自動試探，可「立即試探」或「標為可用」 |
| 日誌 | 系統訊息；每間房的細節在房間的「最近發生的事」 |
| 設定 | 同時運作的房間上限、對話用量換手門檻（0.5～0.9）、派工成本權重、危險指令規則（可以貼指令測試會不會命中）、模型設定檔狀態 |

上方列是整個辦公室的狀態（運作中／等你／排隊）；「全部暫停」或在頁面上按 `F8` 會暫停所有運作中與排隊的房間。`Ctrl+K` 打開指令面板（派工、收件匣、跳到某間房）。可以開啟瀏覽器通知：分頁在背景時，收件匣有新項目會跳通知。

### 安全
只綁 `127.0.0.1`；存取碼在 `web_token.local`（`restart.bat` 打開的網址會換成 30 天的 HttpOnly cookie）；會動手的請求另外要 `X-Token` 標頭；Host 必須是本機；CSP 不允許內嵌腳本。危險指令規則命中一律交給你；合併只在你按收件匣的「合併」時執行（`git merge --no-ff`，主 repo 工作目錄必須乾淨）。

## 🏢 辦公室怎麼運作
**派工流程**：在空房間按「＋ 派工」→ 填專案資料夾與目標 → 助理先檢查專案（必須是 git repo、至少一個 commit、工作目錄乾淨，否則馬上給中文錯誤，不會建立半套 worktree）→ 房間依序走「目標 → 驗收條件（你在收件匣按「開工」或寫修改意見）→ 實作與測試 → 審查」→ 審查通過後收件匣出現「合併」項目，你確認才會合併。

**房間與角色**：每間房一位實作者＋一位唯讀審查者（預設不同家）。實作在獨立 worktree（`<專案上一層>\.ai-team-worktrees\<專案名>-<slug>`，分支 `ai-team/<slug>`），不會動你的工作目錄。助理產生的文件放 worktree 的 `docs/ai-team/<slug>/`（spec.md、plan.md、evidence.md），跟功能分支一起提交。代理的回覆一律當資料，不會被當成指令執行。同時運作的房間上限預設 4（設定頁 `team_max_running`），其餘排隊。

**座位**：`kiro`、`agy`（ACP）、`claude`（headless）、`glm`／`glm_high`（同一個 claude CLI，但改走公司 LiteLLM 的 glm-5.3；高思考版另設思考上限）。GLM 座位需要 `litellm.local.json`，且公司 LiteLLM 必須提供 `/v1/messages`（Anthropic Messages 端點）；不通時座位頁會顯示不可用，用「冒煙測試」可看到明確原因。

**模型分工**：權限灰色地帶由 Decision-1（`/v1/systemone`）與 glm-5.3 判斷；危險指令規則（設定頁可測試）命中一律交給你。派工挑座位的成本與能力目錄在 `team_models.json`（每個座位的模型清單、層級與成本；檔案裡有的座位覆蓋內建預設；格式錯誤時退回內建預設）。

**對話用量換手**：對話用量超過 `team_context_rollover`（預設 0.70；設定頁可調 0.5～0.9，後端接受 0.3～0.95）時，助理把進度整理成交接筆記，開新對話接手。

**Serena（可選）**：claude／GLM 座位若找得到 `uvx`，會自動掛上 Serena 程式碼索引（`uvx --from git+https://github.com/oraios/serena serena start-mcp-server`），所以需要先安裝 [uv](https://docs.astral.sh/uv/)；沒有 uv 就略過，不影響其他功能。額外的 Claude plugin 資料夾填在設定 `team_plugin_dirs`（`config.json`，多個以分號 `;` 分隔，不存在的路徑會略過）。

**檔案位置**
| 內容 | 位置 |
|---|---|
| 房間與收件匣（重啟後從這裡恢復；原本運作中的房間會變「暫停」） | `sessions\team\` |
| 用量狀態 | `sessions\team\quota.json` |
| worktree | `<專案上一層>\.ai-team-worktrees\` |
| 模型設定（金鑰，勿提交） | `litellm.local.json` |
| 座位目錄 | `team_models.json` |
| 冒煙測試結果 | `recon\team-smoke-<時間>.json` |

**路徑太長（Windows）**：助理的 git 指令已加 `core.longpaths`；在 Windows 上建立 worktree 時，助理也會在**目標專案的本機設定**寫入 `core.longpaths=true`（`git config --local`，只影響這個 repo），讓代理自己在 worktree 裡跑的 git 也能處理長路徑。若仍出現 Filename too long，請以系統管理員執行 `git config --system core.longpaths true`，或把專案移到較短的路徑。

**Claude／GLM 座位能跑哪些指令**：這兩種座位以 headless 模式（`claude -p`）執行，實作座位用 `acceptEdits`（可直接改檔）並以 `--allowedTools` 只放行：`git status／diff／log／add／commit`、這間房的測試指令（只限允許清單裡的測試工具，例如 `pytest`、`python -m pytest`、`npm test`、`go test`；`python -c`、`node`、`bash`、`npx` 這類可執行任意程式碼的指令不放行，以直譯器開頭的 `test_cmd` 即使積極模式也要你確認）與唯讀小工具（`ls`、`cat`、`type`、`dir`、`findstr`、`grep`）。其他指令（安裝套件、下載、`git push` 等）一律被拒絕——headless 模式沒辦法當場問你，代理卡住時房間會在收件匣問你怎麼處理（例如你手動安裝相依套件後按「再試一次」）。危險指令清單永遠優先（命中就拒絕）。審查座位維持唯讀（plan 模式）。給代理的提示詞（包括含大段 diff 的審查提示詞）一律經 stdin 送出，不放在命令列，避免 Windows 命令列長度上限與 `.cmd` 外殼在換行處截斷。

**驗收指令的安全檢查**：代理寫的 `test_cmd` 與每條驗收條件的 `check` 會以 shell 執行，所以存下驗收規格時會先比對危險指令規則；命中時即使是積極模式也會在收件匣請你確認（「驗收指令命中危險規則：…」），測試關卡也不會執行命中規則的指令。

## 公司網路（SSL 檢查）
公司網路若做 TLS 檢查，kiro-cli 等 CLI 自帶的 TLS 不認得公司根憑證（錯誤像 `InvalidCertificate(UnknownIssuer)`），kiro 會把它顯示成「Sign in to Kiro」，其實不是沒登入。辦公室（與冒煙測試）啟動 CLI 時會自動把 Windows 憑證存放區（ROOT＋CA）匯出成 `sessions\windows-ca.pem`（超過 24 小時才重新產生），並設成 `SSL_CERT_FILE`／`NODE_EXTRA_CA_CERTS`（你自己已設定這兩者之一就不動）；kiro 以 `acp --agent-engine v3 --auth-method cli` 啟動。冒煙測試開頭會印「公司網路憑證：<路徑>（已套用）」或「未套用（原因）」，遇到連線／憑證類錯誤會歸類為「連不上服務」。

仍失敗時，用 `tools\acp_trace.bat --win-ca` 記錄 ACP 對話（記錄在 `recon\acp-trace-<時間>.log`），把記錄交給助理，或請 IT 提供公司代理設定。

## 🧪 冒煙測試（真 CLI）
第一次正式使用前，先確認 kiro-cli／agy／claude 與 GLM 座位真的能用。測試只在拋棄式 sandbox 裡建立一個小檔案，不碰你的專案：

```
tools\team_smoke.bat                       找得到的 CLI 全測（雙擊也可以）
tools\team_smoke.bat --only kiro           只測 kiro（也可 agy、claude）
tools\team_smoke.bat --seat glm            測 GLM 座位（--seat glm_high 測高思考）
tools\team_smoke.bat --probe-all           每個座位送一則極短訊息，印出各座位用量狀態
tools\team_smoke.bat --sandbox D:\x        指定 sandbox
python tools\team_smoke.py --help          所有參數
```
- 預設 sandbox 是 buttonAuto 上一層的 `ai-team-sandbox`。sandbox 只接受空資料夾或含 `.ai-team-sandbox` 標記檔的資料夾（否則拒絕，避免弄亂別的資料夾）；不存在就建立、`git init` 並只提交 README.md 與標記檔。若是舊版工具建立的 sandbox，在裡面建一個空檔 `.ai-team-sandbox` 即可。
- GLM 座位用 `%TEMP%\ats-xxxx` 的暫存 repo。
- 每個 CLI 印出：啟動與交握、收到的事件數、權限請求與決定、stopReason、檔案是否存在且內容正確（claude 與 GLM 座位另外要求代理 `git commit`，並檢查 `git log` 看得到這個提交，確認 `--allowedTools` 放行有效），最後 ✅／❌；失敗會給中文原因與建議（例如「請在命令提示字元執行 kiro-cli login」、「公司 LiteLLM 沒有 /v1/messages，GLM 座位不可用」）。
- 權限請求照辦公室的規則判斷（有 `litellm.local.json` 用 Decision-1＋glm；讀不到時 edit 類允許、execute 類拒絕）；每個 CLI 逾時 300 秒。
- 失敗時另印「原始訊息」（代理的錯誤、stderr 尾段、被回絕的代理請求方法，金鑰已遮蔽，最多 1500 字），同樣寫進結果檔的 `detail`。
- 冒煙測試不會啟用 Serena 與 `team_plugin_dirs`，只驗證 CLI 本身與 GLM 端點。
- 全部成功結束碼 0，否則 1（`--probe-all` 有座位失敗也是 1；找不到而略過的 CLI 會列在總結）。金鑰不會印出或寫進結果檔。
- `--cli-override 名稱=指令` 是測試用參數（自動測試用假 CLI 取代真的，不連網、不呼叫真 CLI）。

## 檔案
| 檔案 | 用途 |
|---|---|
| `restart.bat`、`tools/assistant_ctl.ps1` | 重新啟動（關掉舊的 → 啟動後端 → 開瀏覽器） |
| `server.py` | 網頁後端：`/api/team/*` REST API、SSE 即時推送、設定 |
| `team/` | 辦公室：房間排程、各家 CLI 的驅動、權限判斷、派工、worktree 與合併 |
| `tools/team_smoke.bat`、`tools/team_smoke.py` | 真 CLI 冒煙測試（見上） |
| `web/console/` | 主控台（純 HTML/CSS/JS，不需要建置）；`office.js`／`office.css` 是辦公室 |

## 舊的 IDE 視窗自動化（已停用）
早期版本用 UI Automation 盯著 Kiro／Antigravity 的 IDE 視窗代按、代打字，另外有基本按鈕精靈（UIA／圖片比對）與 Next.js 像素辦公室（`/office/`）。這些頁面與 API 已從主控台移除，後端也不再啟動 UIA 輪巡、F8 全域熱鍵與圖片比對；相關模組（`automation.py`、`office_bridge.py`、`pixeloffice/`、`main.py` 等）暫時留在磁碟上，但不再被引用。
