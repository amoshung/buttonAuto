# Windows 智慧按鈕精靈 (ButtonAuto) 設計規格書

## 1. 專案目標與概述
開發一款執行於 Windows 的輕量級桌面「按鈕精靈」工具（Python + Tkinter + OpenCV + mss + Win32 API），預設每秒（可調）掃描螢幕上的指定區域（或全螢幕），一旦發現與目標清單（預設包含深色與淺色背景的 `Submit` 按鈕，並支援使用者自行截圖新增多個目標按鈕）相符的按鈕，立即將滑鼠游標瞬移至按鈕中心點完成左鍵點擊，並在約 5~10ms 內瞬移回使用者原本的滑鼠座標，不干擾使用者正常操作。

## 2. 核心功能需求
1. **極速多區域螢幕掃描**：
   - 預設每 `1.0` 秒掃描一次（可於 UI 調整掃描間隔，範圍 `0.1s` ~ `10.0s`，並提供快捷預設值）。
   - 支援設定**多個掃描區域框 (Scan Regions)**：若未設定任何區域則掃描全螢幕（主螢幕或虛擬全螢幕）；若有設定則僅針對各個掃描框擷取畫面並進行比對。
   - 支援在螢幕上顯示**滑鼠穿透 (Click-through) 的半透明綠色外框**，標示目前的掃描範圍，並可隨時切換顯示或隱藏。
2. **多目標按鈕圖片管理**：
   - 內建使用者提供的兩張 `Submit` 按鈕圖片（`targets/submit_dark.png` 與 `targets/submit_light.png`）。
   - 提供「`+ 截圖新增目標`」功能，允許使用者直接在螢幕上連續拉框截取多個目標按鈕，自動儲存至 `targets/` 並加入比對清單。
   - 懸浮面板上顯示目標按鈕清單（含縮圖、名稱、啟用勾選框、刪除按鈕）。
   - 支援調整 OpenCV 模板比對相似度門檻（預設 `0.85`，支援多尺度微幅縮放 `0.9x ~ 1.1x` 或標準 `1.0x` 極速比對切換）。
3. **無感瞬移點擊與防誤觸機制**：
   - 啟動時呼叫 Windows API 設定 Per-Monitor DPI 感知（`SetProcessDpiAwarenessContext` / `SetProcessDPIAware`），確保高 DPI 縮放（125%、150% 等）下截圖像素與滑鼠點擊座標 100% 精準對應。
   - 點擊時記錄當前游標位置 `(orig_x, orig_y)` -> 移動至目標中心 `(tx, ty)` -> 觸發左鍵按下與放開 -> `8ms` 後移回 `(orig_x, orig_y)`。
   - **自我排除保護**：掃描時自動檢測匹配的座標是否落在懸浮控制面板視窗範圍內，若是則直接忽略，避免點擊到面板上的按鈕縮圖。
   - **全域熱鍵 (`F8`)**：使用 Win32 `RegisterHotKey` 或背景鍵盤狀態偵測（`GetAsyncKeyState(VK_F8)`），讓使用者在任何時候按 `F8` 都能立即切換「啟動 / 停止」。

## 3. 系統模組架構
```
d:\_AI_Projs\buttonAuto\
├── main.py               # 程式進入點（設定 DPI 感知、初始化預設圖片與啟動 UI）
├── config.py             # 設定管理模組（讀寫 config.json，管理區域與目標清單）
├── clicker.py            # Win32 原生瞬移點擊與全域熱鍵偵測模組
├── scanner.py            # mss 螢幕截圖與 OpenCV 模板匹配引擎
├── region_selector.py    # 全螢幕互動式多框選取工具 & 滑鼠穿透範圍顯示框
├── overlay_ui.py         # Tkinter 置頂懸浮控制面板
├── targets/              # 存放目標按鈕截圖的資料夾
│   ├── submit_dark.png
│   └── submit_light.png
├── requirements.txt      # Python 依賴套件清單 (opencv-python, mss, pillow, numpy)
└── run.bat               # Windows 雙擊一鍵啟動腳本（自動檢查與安裝套件）
```

## 4. 資料結構 (`config.json`)
```json
{
  "scan_interval": 1.0,
  "threshold": 0.85,
  "multi_scale": false,
  "show_region_borders": true,
  "cooldown_sec": 0.8,
  "regions": [
    {"x": 100, "y": 200, "w": 500, "h": 400, "enabled": true}
  ],
  "targets": [
    {"name": "Submit (深色)", "path": "targets/submit_dark.png", "enabled": true},
    {"name": "Submit (淺色)", "path": "targets/submit_light.png", "enabled": true}
  ]
}
```

## 5. 錯誤處理與測試驗證
- **模板尺寸大於掃描區域**：當某個掃描區域框比目標按鈕圖片還小時，自動跳過該區域與該模板的比對，防止 OpenCV 拋出例外。
- **圖片檔案遺失**：啟動或掃描時若某張目標圖片檔案不存在，自動標記並不中斷掃描迴圈。
- **單元與整合測試**：撰寫自動化測試驗證：
  1. `config.py` 的讀寫與預設值初始化。
  2. `scanner.py` 在合成畫面上對深色/淺色 `Submit` 按鈕的定位精準度與自我視窗排除邏輯。
  3. `clicker.py` 點擊後游標正確歸位。

