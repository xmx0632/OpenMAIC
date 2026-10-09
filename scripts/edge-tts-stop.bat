@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM edge-tts proxy service one-click stopper
REM Finds the process listening on 127.0.0.1:8500 and kills it.
REM If the service is not running, exits without doing anything.
REM Logs: %TEMP%\edge-tts-server.log / edge-tts-server-err.log
REM ============================================================

set "HEALTH=http://127.0.0.1:8500/health"

REM ---- Probe whether the service is running ----
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH%' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }"
if !errorlevel! neq 0 (
    echo [edge-tts] Service is not running. Nothing to stop.
    %SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
    exit /b 0
)

REM ---- Kill the listener process on port 8500 ----
echo [edge-tts] Stopping service...
powershell -NoProfile -Command "$pids = Get-NetTCPConnection -LocalPort 8500 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique; foreach($p in $pids){ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; Write-Output ('[edge-tts] Killed PID ' + $p) }"

REM ---- Verify it is down ----
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH%' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }"
if !errorlevel! neq 0 (
    echo [edge-tts] Stopped OK. Port 8500 is free.
    %SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
    exit /b 0
)

echo [edge-tts] WARNING: service still responding on %HEALTH%. Kill it manually:
echo [edge-tts]   netstat -ano ^| findstr :8500
echo [edge-tts]   taskkill /PID ^<pid^> /F
pause
exit /b 1
