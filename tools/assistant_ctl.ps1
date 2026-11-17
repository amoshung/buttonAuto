<#
  AI 工程助理：停止／等待後端（restart.bat 使用）
    assistant_ctl.ps1 stop   先請舊的後端自己關閉，再強制結束這個資料夾裡殘留的助理程序（含舊的桌面面板 main.py）
    assistant_ctl.ps1 wait   等新的後端啟動完成（/healthz 回應），失敗時印出 error.log 末段
#>
param(
  [Parameter(Mandatory = $true)][ValidateSet('stop', 'wait')][string]$Action,
  [int]$Timeout = 45
)
$ErrorActionPreference = 'SilentlyContinue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path.TrimEnd('\')
$runtime = Join-Path $root 'runtime.local.json'
$errlog = Join-Path $root 'error.log'
$rootLower = $root.ToLower()

function Read-Runtime {
  if (Test-Path $runtime) { try { return Get-Content $runtime -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }
  return $null
}

function Get-AssistantProcesses {
  # 這個資料夾裡的 server.py／main.py／capture_tool.py（用完整路徑比對，不會誤殺其他 Python 程式）
  # 注意：PowerShell 的 -and／-or 優先順序相同，一律加括號
  Get-CimInstance Win32_Process | Where-Object {
    $cl = [string]$_.CommandLine
    $mine = ($_.ProcessId -ne $PID) -and ($cl.Length -gt 0) -and ($cl.ToLower().Contains($rootLower))
    $isPy = ($_.Name -match '^pythonw?[\d.]*\.exe$') -and ($cl -match '(server|main|capture_tool)\.py')
    $isOldBat = ($_.Name -eq 'cmd.exe') -and ($cl -match '\\run\.bat')
    $mine -and ($isPy -or $isOldBat)
  }
}

if ($Action -eq 'stop') {
  $rt = Read-Runtime
  if ($rt) {
    Write-Host "  請舊的後端（PID $($rt.pid)、連接埠 $($rt.port)）自己關閉…"
    try {
      Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($rt.port)/api/shutdown" -Headers @{ 'X-Token' = $rt.token } `
        -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
      Wait-Process -Id $rt.pid -Timeout 6
    } catch { }
  }
  $left = @(Get-AssistantProcesses)
  foreach ($p in $left) {
    Write-Host "  結束殘留的程序 PID $($p.ProcessId)：$($p.Name)"
    Stop-Process -Id $p.ProcessId -Force
  }
  Start-Sleep -Milliseconds 400
  Remove-Item $runtime -Force
  if (-not $rt -and $left.Count -eq 0) { Write-Host '  沒有正在執行的助理。' }
  exit 0
}

# wait
$start = Get-Date
Write-Host '  等待後端啟動…'
while (((Get-Date) - $start).TotalSeconds -lt $Timeout) {
  Start-Sleep -Milliseconds 700
  $rt = Read-Runtime
  if ($rt) {
    try {
      $h = Invoke-RestMethod -Uri "http://127.0.0.1:$($rt.port)/healthz" -TimeoutSec 2
      if ($h.ok) {
        Write-Host ''
        Write-Host "  AI 工程助理已啟動：http://127.0.0.1:$($rt.port)/  （版本 $($h.version)）"
        Write-Host '  主控台已在預設瀏覽器打開；後端在最小化的「AI Assistant Backend」視窗，關掉那個視窗就會停止助理。'
        exit 0
      }
    } catch { }
  }
  if ((Test-Path $errlog) -and ((Get-Item $errlog).LastWriteTime -gt $start)) { break }
}
Write-Host ''
Write-Host '  後端沒有在時間內啟動。' -ForegroundColor Red
if (Test-Path $errlog) {
  Write-Host '  error.log 最後幾行：'
  Get-Content $errlog -Tail 25 -Encoding UTF8 | ForEach-Object { Write-Host "    $_" }
}
Write-Host '  也可以手動執行：python server.py --open 看完整訊息。'
exit 1
