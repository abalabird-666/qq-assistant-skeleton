# restart_qq_watch.ps1 -- R5-3 deployment helper
# ONLY kill the qq_watch process recorded in qq_watch.pid.
# NEVER use `Stop-Process -Name pythonw`: that took down both NapCat instances on 2026-11-10.
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Home2   = "C:\Users\yourname\.dsh\qq-duty"
$PidFile = Join-Path $Home2 "qq_watch.pid"
$Vbs     = "C:\Users\yourname\.dsh\startup\qq-watch.vbs"
$Py      = "C:\Users\yourname\.dsh\bili-duty\.venv\Scripts\pythonw.exe"
$Script  = Join-Path $Home2 "qq_watch.py"

function Get-QqWatchProc {
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*qq_watch.py*' }
}

Write-Host "== 1. read pidfile =="
$target = $null
if (Test-Path $PidFile) {
    $pidText = (Get-Content $PidFile -Raw).Trim()
    Write-Host "   qq_watch.pid = $pidText"
    if ($pidText -match '^\d+$') {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$pidText" -ErrorAction SilentlyContinue
        if ($p -and $p.CommandLine -and $p.CommandLine -like '*qq_watch.py*') {
            $target = [int]$pidText
        } else {
            Write-Host "   pid $pidText is gone or not qq_watch (pid reuse guard) -> fall back to cmdline match"
        }
    }
} else {
    Write-Host "   no pidfile -> fall back to cmdline match"
}
if (-not $target) {
    $c = Get-QqWatchProc | Select-Object -First 1
    if ($c) { $target = [int]$c.ProcessId }
}

Write-Host "== 2. kill qq_watch only =="
if ($target) {
    $napcatBefore = @(Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='NapCatWinBootMain.exe'" |
                      Where-Object { $_.CommandLine -like '*napcat*' }).Count
    Write-Host "   napcat processes before: $napcatBefore"
    if ($DryRun) {
        Write-Host "   [dry] would kill pid $target"
    } else {
        Stop-Process -Id $target -Force
        Start-Sleep -Seconds 3
        Write-Host "   killed pid $target"
    }
    $after = Get-CimInstance Win32_Process -Filter "ProcessId=$target" -ErrorAction SilentlyContinue
    Write-Host "   still alive? $([bool]$after)"
} else {
    Write-Host "   nothing to kill"
}

Write-Host "== 3. start qq_watch =="
if ($DryRun) {
    Write-Host "   [dry] would run: cscript //nologo `"$Vbs`""
} else {
    & cscript.exe //nologo $Vbs
    Start-Sleep -Seconds 6
}

Write-Host "== 4. verify =="
$new = Get-QqWatchProc | Select-Object -First 1
if ($new) {
    Write-Host "   qq_watch running: pid $($new.ProcessId)"
} else {
    Write-Host "   !! qq_watch NOT running"
}
$napcat = Get-CimInstance Win32_Process -Filter "Name='QQ.exe' OR Name='NapCatWinBootMain.exe'" |
          Where-Object { $_.CommandLine -like '*napcat*' }
$bot = @($napcat | Where-Object { $_.CommandLine -like '*napcat-setup*' })
$me  = @($napcat | Where-Object { $_.CommandLine -like '*napcat-me*' })
Write-Host "   napcat bot processes: $($bot.Count)  | me processes: $($me.Count)"
foreach ($port in 3001, 3002) {
    $l = (netstat -ano | Select-String -Pattern ":$port\s+0\.0\.0\.0:0\s+LISTENING")
    Write-Host ("   port {0} listening: {1}" -f $port, [bool]$l)
}
