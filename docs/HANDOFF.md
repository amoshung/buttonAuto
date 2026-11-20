# 交接文件（新對話從這裡接續）

更新：2026-11-17 16:40（台北）

## 目標
把 buttonAuto 做成「像素辦公室」：一個專案一間房，公司 glm-5.3／Decision-1 扮演敏捷角色，指揮 kiro-cli、agy、claude 與 GLM 座位在獨立 git worktree 把任務做到「已驗證、等你合併」。下一階段目標：使用者可以**全權交付**——保證往前走、角色專業、能力庫。

## 新對話要做的事（使用者 16:29 指示：在別的 chat 執行設計文件）
1. 讀 `CLAUDE.md`（使用者偏好：繁體中文、不壓縮對話、70% 存進度開新對話）。
2. 讀已核准的設計：`docs/superpowers/specs/2026-11-17-autonomy-roles-capabilities-design.md`（使用者 16:36 核准，含 §7 全部判斷）。它包含並取代計畫 Task 21 的部分內容（見設計 §5.2）。
3. 用 superpowers:writing-plans 產生新計畫 `docs/superpowers/plans/2026-11-18-autonomy-roles-capabilities.md`，再用 superpowers:subagent-driven-development 執行（新的 ledger）。
4. 舊計畫 Task 21 的文字仍可參考（`docs/superpowers/plans/2026-11-17-multi-cli-office.md` 末尾），但以設計文件為準。

## 文件
- 主規格：`docs/superpowers/specs/2026-11-17-multi-cli-team-design.md`（附錄 A、B）
- 新設計：`docs/superpowers/specs/2026-11-17-autonomy-roles-capabilities-design.md`
- 舊計畫：`docs/superpowers/plans/2026-11-17-multi-cli-office.md`（Task 1–21）
- 舊 ledger（含所有 Ruling）：`.superpowers/sdd/2026-11-17-multi-cli-office/progress.md`（git 忽略，只在原工作副本）

## 已完成（分支 feat/multi-cli-office）
Task 1–20：ACP／headless 驅動、GLM 座位、模型路由與派工、權限政策、worktree、關卡、角色、房間、辦公室、API、像素辦公室畫面（精緻房間、對話泡泡、懸浮說明）、模型選擇、用量透明、Kiro v3 模型、自動派工只用 Claude／GLM、時間軸可展開細節（Task 19＋修正）、每房代管開關（Task 20）。

## 執行設計前的收尾（舊對話負責，完成後同步到 D 槽並以 sha 驗證）
- Task 20 修正第 1 輪（second_opinion／choose 改用 `_Fence`；關掉代管後項目回收件匣的行為測試；runner 結束錯誤項目順序測試）。
- Task 19 修正第 2 輪（`team/events.py` `_KEYISH` 正規表示式二次回溯效能、tokenizer_config 過度遮蔽、細節讀取失敗卡住）。
- 若新對話開始時這兩項的 commit 還不在 D 槽，先檢查 `git log` 是否有 "Task 20 fix" 與 "_KEYISH" 相關 commit。

## 已知環境限制
- 公司 TLS 攔截：所有 CLI 子程序要帶 Windows 根憑證（`team/netcerts.py`、`drivers.with_cert_env`）。
- Windows 長路徑：git 一律 `core.longpaths=true`。
- 已知僅環境相關的測試失敗：test_dashboard ×2、test_end_to_end、test_scanner。

## 使用者偏好
見 `CLAUDE.md`。

## 2026-11-17 17:xx 新對話進度（執行設計文件的對話）
- 已完成：步驟 3 前半——用 superpowers:writing-plans 產生新計畫 `docs/superpowers/plans/2026-11-18-autonomy-roles-capabilities.md`（18 個 Task；設計對照表在文末）。
- 尚未開始執行（subagent-driven-development），原因：
  1. 這台電腦的 Windows 更新（9/8）讓 Claude 的工作區 shell 掛載不到 D 槽，無法在本機跑 git、pytest。
  2. D 槽 `.git` 只有 `main`（最後 commit 是 11/16 的 UI 重設計），沒有 `feat/multi-cli-office` 分支；Task 1–20 的成果在 D 槽是未提交的同步檔案。GitHub（amoshung/buttonAuto）存取被拒，無法用 sha 驗證前置修正。
  3. 從檔案內容看：`models.second_opinion` 已用 `Fence`、`_KEYISH` 已是有界量詞，Task 20／19 修正應已同步，但未以 git 驗證。
- 下一步：使用者審閱計畫，並決定執行方式（見計畫開頭 Global Constraints 的前置條件）。

## 2026-11-18 16:00 像素辦公室 45° 等距無前牆日系古民家高精細美術優化 ＋ 24 角色庫 ＋ 16 種四季天氣房間風景與擺設（已完成）
- **規格與計畫**：
  - 設計規格書：`docs/superpowers/specs/2026-11-18-isometric-cozy-pixel-office-design.md`
  - 實作計畫：`docs/superpowers/plans/2026-11-18-isometric-cozy-pixel-office.md`
- **已完成項目**：
  1. 安裝前端與等距像素美術 Skills：`~/.gemini/config/skills/frontend-design/SKILL.md`、`~/.gemini/config/skills/isometric-pixel-art/SKILL.md`。
  2. 產製《日本田舎暮らし》與開羅風格高解析 45° 等距無前牆古民家基底圖（`iso-room-clean.png`）與去背透明 24 角色正反面精靈圖集（`roster-humans-12.png`、`roster-humans-back-12.png` 12 位人類 ＋ `roster-animals-12.png`、`roster-animals-back-12.png` 12 位可愛動物吉祥物），存放於 `pixeloffice/public/pixel-assets/` 並匯出至 `web/pixel/pixel-assets/`。
  3. 角色分工與零重複機制（`pixeloffice/lib/pixelRoomScene.ts`）：
     - 3 位實際工作角色（`supervisor`、`implementer`、`reviewer`）100% 全部使用人類角色（`species === 'human'`），依工作狀態在雙螢幕木桌（3/4 背面坐姿＋辦公椅背前景遮罩、打字掃視螢幕動畫）、敏捷看板（3/4 背面貼便利貼與轉身說明）與圍爐裏坐墊（盤腿舉杯喝抹茶動畫）之間沿走廊 L 型尋路移動。
     - 2 位房間吉祥物夥伴（`companions`）100% 全部使用動物角色（`species === 'animal'`），體型明顯小於人類（約人類 50% 高度），不顯示任何頭頂標記，並在左前杉木走廊與右前榻榻米角落自主進行散步、小跳躍、嗅聞、打盹等生動動作，絕不遮擋開發者視線。
     - 透過 `registerVisibleRooms` 與無碰撞分區，確保單一房間與多房間同畫面（如 2×2 共 20 位角色）絕不出現重複角色。
  4. 16 種房間風景與屋內擺設主題（`ROOM_THEMES`）：涵蓋春櫻漫舞、春雨綿綿、盛夏新綠、夏夜雷雨（含閃電與室內雷光）、湘南夏海、夏夜螢火、深秋楓紅、秋收月見、銀杏金風、山霧朝露、寒冬瑞雪（含暖桌被爐 Kotatsu 與蜜柑）、吹雪極夜、雪見溫泉、茜色夕照、星夜極光、嵐山竹林；屋內座布團配色、地爐／被爐與角落家具（櫻花盆栽、和傘架、西瓜盤與旋轉電扇、蚊香豬、晴天娃娃、黑膠唱機、天文望遠鏡、枯山水盆景等）隨主題同步變換。
  5. 電腦螢幕橫條精準裁切（`MONITOR_SPECS`）：量測 4 台顯示器黑色面板四角座標並搭配 `ctx.clip()` 與面板斜率繪製程式碼橫條，100% 貼合於螢幕內框。
  6. 遊戲化輕量漫畫對話框與狀態圖標（`pixeloffice/components/RoomGrid.tsx`）：
     - 1×1 模式下顯示可點擊的小巧漫畫對話框 `[⚡ ...]`（點擊展開完整對話框與 5 筆最新工作紀錄）搭配半透明防遮擋單行角色名稱與狀態。
     - 2×2／3×3 多格模式下僅顯示小巧漫畫狀態圖標（忙碌 `⚡`、規劃 `📋`、閒置 `☕`、等待 `❓`、錯誤 `⚠️`），完全不遮擋像素人物。
  7. **48 角色大擴充（24 位台灣在地＋日系人類 ＆ 24 種純天然四足犬貓品種）與物理互動升級（2026-11-19）**：
     - **吉祥物 100% 去擬人化，擴充為 24 種純天然四足犬貓品種**：
       - **12 種狗狗品種**（`roster-dogs-12.png`）：台灣犬（黑土狗小黑）、赤柴、黑柴、威爾斯柯基、黃金獵犬、邊境牧羊犬、西伯利亞哈士奇、法國鬥牛犬、短毛臘腸犬、泰迪紅貴賓、比熊犬、三色米格魯。
       - **12 種貓咪品種**（`roster-cats-12.png`）：台灣虎斑米克斯、台灣大橘貓、三花貓、台灣黑貓（歐嚕嚕）、黑白賓士貓、玳瑁貓、暹羅貓、英短藍貓、美短銀虎斑、波斯白貓、雙色布偶貓、曼赤肯短腿貓。
       - **品種專屬物理動畫**：四足交替小跑、柯基／法鬥吐司臀趴睡搖屁股（`corgi_sploot`）、牧羊犬／台灣犬敏捷巡邏（`herding_dash`）、貴賓／比熊輕快彈跳步（`poodle_prance`）、大橘貓／英短折手農夫揣呼嚕（`cat_loaf`）、虎斑／黑貓壓低身體搖晃屁股撲球（`cat_pounce`）、三花／賓士舔爪洗臉理毛（`cat_groom`）、曼赤肯短腿立姿張望（`munchkin_stand`）。
     - **新增 12 位台灣在地特色人類角色**（`roster-humans-tw-12.png` ＆ `roster-humans-tw-back-12.png`，合計 **24 位人類工作角色**）：
       - 竹科手搖飲工程師「阿哲」、台灣水電工地主任「阿欽伯」、夜市美食達人「雅婷」、宮廟文創潮牌設計師「家豪」、大稻埕茄芷袋文青 PM「詩涵」、台鐵鐵道迷調度官「鐵雄」、百岳登山山友 SRE「山羌哥」、西瓜皮安全帽機車通勤駭客「冠宇」、客家花布 UI 總監「曉彤」、原住民圖騰資安專家「巴奈」、台南老宅藍白拖功夫茶 QA「阿甘叔」、超商萬能店長 Scrum「小綠」。
     - **多節點等距物理尋路（`buildIsoWalkPath`）與人寵互動**：
       - 角色於工作桌、看板與圍爐裏之間移動時，自動繞行雙螢幕木桌與中央炭火地爐／被爐，絕不穿模踩過火爐，並依路徑總長維持恆定物理步速。
       - 狗狗會追逐滾動網球、前往左前木製雙碗架低頭吃飼料喝水、到工作中工程師椅旁忠心陪伴，或向休息中的人類討摸摸（人類俯身摸摸＋冒出愛心 `♥`）；貓咪會在榻榻米農夫揣打盹、到右前劍麻貓抓板搖屁股磨爪撲毛線球，並向茶歇中的主管撒嬌呼嚕。
     - **補齊台灣科技辦公室與寵物生活道具（`drawOfficeLifeAndPetProps`）**：
       - 主機上方擺放台灣工程師鎮機神物**「綠色乖乖」**（隨房間正常／等待／報錯切換綠／黃／紅狀態指示燈）、雙螢幕木桌上擺放**「台灣波霸珍珠奶茶」**、榻榻米階梯處擺放**「藍白拖」**，以及左前**木製雙碗寵物飲食架**、**圓形軟墊寵物睡窩**、右前**劍麻貓抓板與毛線球**、走廊**物理滾動玩具網球**。
  8. 測試與建置：`npm test`（9 項測試全數通過）與 `npm run build`（靜態匯出至 `web/pixel/`）皆已完成驗證。

## 2026-11-19 SSH 遠端開發能力 ＋ 遠端 CLI（claude / agy / kiro-cli）嚴格握手偵測與不可調用自動隱藏（已完成）
- **規格文件**：`docs/superpowers/specs/2026-11-18-ssh-remote-cli-design.md`
- **已完成項目（U1–U5）**：
  1. **SSH 主機管理、防注入驗證與遠端單檔 Python 探針（`team/ssh.py`、`tests/team/test_ssh.py`）**：
     - 自動解析本機 `~/.ssh/config`（含 `Include`）非萬用字元 `Host` 別名，並結合 `ssh_hosts.local.json`（已加入 `.gitignore`）支援自訂 `user@host[:port]` 新增／移除與探測快取。
     - `validate_ssh_target` 嚴格阻擋 `-oProxyCommand=...` 等 SSH 參數注入與空白／控制字元。
     - `REMOTE_PROBE_SCRIPT` 透過系統 OpenSSH（`ssh -T -o BatchMode=yes`）對遠端或本機的 `claude`、`agy`、`kiro-cli` 執行零副作用嚴格握手檢測（`kiro`／`agy` 驗證 ACP `initialize` + `session/new` 並擷取可用模型；`claude` 驗證 `--version` 與 headless `stream-json` `system/init`）。握手失敗、未登入或未安裝的 CLI 一律標記 `callable: False` 並從 `clis` 與 `available_seats` 剔除。
  2. **遠端 Git Worktree 與測試檢查（`team/gitops.py`、`team/checks.py`、`tests/team/test_ssh_gitops.py`）**：
     - `Worktree.ssh_host` 與 `gitops` 全系列函式（`git`、`ensure_local_git_repo`、`create_worktree`、`merge_into_repo`、`remove_worktree` 等）及 `checks`（`run_cmd`、`test_gate`、`quick_check`）支援透過 SSH 在遠端專案目錄執行。
  3. **遠端 CLI Stdio 長連線轉發與環境變數安全封包（`team/ssh.py`、`team/drivers.py`、`team/acp.py`、`team/headless.py`、`team/notes.py`、`team/runner.py`、`tests/team/test_ssh_drivers.py`）**：
     - `wrap_remote_stdio_cmd` 透過 `LOCAL_STDIO_BRIDGE_SCRIPT` 與遠端 `REMOTE_STDIO_STUB` 建立雙向無緩衝 stdio 管道，並由 stdin 首行 JSON 傳遞 `ANTHROPIC_AUTH_TOKEN` 等機密環境變數，絕不在遠端 `ps aux` 洩漏金鑰；SSH 斷線時遠端處理程序群組自動清理。
  4. **Room、Office 與 Server API 整合（`team/rooms.py`、`team/office.py`、`server.py`、`tests/test_server_ssh.py`）**：
     - `Room.ssh_host` 持久化，新增 `GET/POST /api/team/ssh/hosts`、`POST /api/team/ssh/probe`，並擴充 `/api/team/dirs`、`/api/team/scan`、`/api/team/models`、`/api/team/rooms` 支援 `ssh_host`；嘗試綁定不可調用的 CLI 時由後端明確拒絕。
  5. **Pixel Office 前端介面與嚴格 CLI 過濾（`pixeloffice/` -> `web/pixel/`、`tests/test_pixel_ui.py`）**：
     - `DispatchWizard.tsx` 新增「🖥️ 本機開發 ／ 🌐 SSH 遠端開發」切換、`~/.ssh/config` 與自訂 `user@host[:port]` 主機管理、遠端資料夾瀏覽與專案掃描。
     - `DispatchWizard.tsx` 與 `SessionPanel.tsx` 透過 `filterCallableSeatChoices` 嚴格過濾 CLI 選單與狀態標籤，不能調用的 CLI 絕不顯示；遠端房間於 `SessionPanel` 抬頭顯示 `🌐 SSH 遠端：<host>` 標章。

## 2026-11-19 像素辦公室物理引擎整合（Matter.js + Pathfinding A* + SunCalc + Simplex-Noise）與物種動作／多窗景／Hover 名牌升級（已完成）
- **現成物理與天文套件**：安裝並直接套用 `matter-js`（2D 剛體碰撞、阻尼、玩具球反彈）、`pathfinding`（A* 網格尋路與椅前入座錨點）、`suncalc`（台灣經緯度 24 小時太陽高度／方位角與動態投射斜影）、`simplex-noise`（火焰浮力、咖啡熱氣捲曲、天氣風場）。
- **核心模組**：
  - `pixeloffice/lib/roomPhysics.ts`：獨立物理世界與固定子步進（`<= 16.667ms`），解耦 React render 副作用避免後端派工指令觸發瞬移或穿模；物種原生姿勢鎖定（黑貓 `loaf`、赤柴／大橘等 `sit` 嚴格零位移）；站姿犬貓支援 `loaf → waking → stretching → walking/trotting` 智慧 Utility AI 與腳步驅動四足步態（`QuadrupedGaitState`）；人類支援平滑 `standing_up → A* walking → seating → seated` 與閒置自主活動。
  - `pixeloffice/lib/roomAtmosphere.ts`：4 種可選窗景（都會高樓天際線／古民家庭園／海港遠眺／高山雲海）、SunCalc 室內日光束與斜影、季節連動中央配置（夏天收起火爐改為和室木茶几＋西瓜＋冰麥茶；春秋冬為 Simplex-Noise 動態火焰與火星）、覆蓋底圖靜態指針的即時掛鐘（含紅秒針）、手沖咖啡與手沖壺熱氣。
  - `pixeloffice/lib/pixelRoomScene.ts`：離屏 Canvas 自動量測精靈腳底線 `footRatio` 根除寵物浮空；四足前後肢分離跨步取代整圖抖動；人類坐座墊改用柔和坐姿壓縮與 AO 陰影根除「插進坐墊」切口。
  - `pixeloffice/components/RoomGrid.tsx`：人物名稱與對話框改為滑鼠 Hover 人物本體才緊貼頭頂浮現（平常完全隱藏），點擊即可檢視工作紀錄；房間標題列新增「窗景」、「季節與室內配置」、「時段日照」三個下拉選單（每房獨立記憶於 `localStorage`）。
- **四季專屬高精細房間與真實多姿態精靈圖集（2026-11-19 補完）**：
  - 4 套四季專屬等距像素房間原圖（`iso-room-spring.png` 春櫻／`iso-room-summer.png` 夏西瓜電扇木茶几／`iso-room-autumn.png` 秋楓地爐／`iso-room-winter.png` 冬格紋暖桌被爐）與 5 套高精細 16-bit 窗景原圖（`vista-skyline.png`、`vista-skyline-night.png`、`vista-harbor.png`、`vista-alpine.png`、`vista-sakura.png`），並支援陰天／雨天／雷雨窗景自動轉陰暗冷灰雲幕與散射天光。
  - 6 套全新專屬姿態精靈圖集：`roster-humans-sit-12.png`、`roster-humans-tw-sit-12.png`（24 位人類在榻榻米座墊呈現盤腿／正坐坐姿，起身走動與白板作業切換完整站姿）；`roster-dogs-rest-12.png`、`roster-cats-rest-12.png`（真實趴臥休息）；`roster-dogs-stretch-12.png`、`roster-cats-stretch-12.png`（真實伸懶腰）；移動時前／後腳掌獨立交替跨步，靜止時僅微微呼吸。
- **驗證**：`npm test`（`office-sim.test.ts` + `roomPhysics.test.ts` 共 14 項測試全數通過）與 `npm run build` 靜態匯出至 `web/pixel/`。

## 2026-11-19 產品體驗 6 大升級（開機健檢精靈、Demo 模式、環境音、房間內快速審核、成本護欄、安全設定匯出／匯入）（已完成）
- **後端（`team/bootstrap.py`、`team/models.py`、`config.py`、`server.py`）**：
  - `GET /api/team/preflight`、`POST /api/team/preflight/test`、`POST /api/team/preflight/save`：啟動時自動檢查本機 CLI（`claude`/`agy`/`kiro-cli`）、Git 與 `litellm.local.json`；支援測試 IP/Port 端點連線與即時熱重載模型設定；放寬 `load_model_config()` 單一金鑰端點（省略 `decision1_key` 自動沿用 `glm_key`）。
  - `GET/POST /api/team/budget` 與 `AppConfig.team_daily_token_limit`：每日 Token 成本護欄，超過上限自動觸發 `pause_all`。
  - `GET /api/team/profile/export`、`POST /api/team/profile/import`：安全匯出／匯入團隊參數與 SSH 主機清單（自動排除所有金鑰）。
- **前端（`SetupWizardModal.tsx`、`OfficeTopBar.tsx`、`RoomGrid.tsx`、`PixelOfficeApp.tsx`、`sound-fx.ts`）**：
  - 首次啟動缺少模型或 CLI 時自動彈出「環境健檢與首次啟動設定精靈」，提供一鍵複製 CLI 安裝指令、Ollama/LiteLLM IP:Port 快速模板、連線測試、一鍵 Demo 展示房間與設定檔匯出／匯入。
  - 頂部列新增「執行中房間數｜今日 Token 成本護欄」膠囊與「微音效／雨天細雨／地爐柴火／靜音」Web Audio 合成環境音切換（任務完成自動響起風鈴聲）。
  - 房間卡片內新增即時權限審核警示條與內嵌快速審核卡片，無需切回右側清單即可直接核准或拒絕。
