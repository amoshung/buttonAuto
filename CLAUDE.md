# 給 Claude 的工作規則（buttonAuto）

## 進度一律寫檔，不靠對話記憶
使用者不希望依賴對話壓縮（浪費 token、會遺失細節）。因此：
- 每完成一個步驟就更新進度檔，讓**新開的對話**只讀檔案就能接手：
  - 實作計畫進度：`.superpowers/sdd/<plan>/progress.md`（SDD ledger）
  - 跨對話交接：`docs/HANDOFF.md`（目前目標、已完成、下一步、未決事項、使用者偏好）
- 對話變長時，主動先更新 `docs/HANDOFF.md`，並建議使用者開新對話、只說「從 docs/HANDOFF.md 接續」。
- 新對話開始時：先讀 `docs/HANDOFF.md` 與 ledger，再用 `git log` 核對，不重做已完成的任務。

## 使用者偏好（摘要，詳見 HANDOFF）
- 繁體中文回覆；喜歡先寫計畫、腦力激盪。
- 模型只用 glm-5.3 與 Decision-1；不用 gemma、muse-glimmer。金鑰只放 `litellm.local.json`（gitignored），不得寫入程式、文件、日誌。
- 不自動合併主線；不碰使用者的 IDE 編輯視窗。
