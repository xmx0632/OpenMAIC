@echo off
setlocal EnableDelayedExpansion

REM ============================================================
REM edge-tts proxy service one-click starter (pure Edge mode)
REM Listens on 127.0.0.1:8500 for OpenMAIC TTS_OPENAI channel.
REM If already running, exits; otherwise starts as a hidden
REM detached process. Logs: %TEMP%\edge-tts-server.log / -err.log
REM ============================================================

set "APP_DIR=D:\workspace\OpenMAIC"
set "LOG=%TEMP%\edge-tts-server.log"
set "ERRLOG=%TEMP%\edge-tts-server-err.log"
set "HEALTH=http://127.0.0.1:8500/health"

REM ---- Probe whether the service is already up ----
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH%' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }"
if !errorlevel! equ 0 (
    echo [edge-tts] Service is already running: %HEALTH%
    %SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
    exit /b 0
)

REM ---- Start as hidden detached process (survives window close) ----
REM Prefer the isolated venv (fastapi/edge-tts/openai live there);
REM fall back to system python so the old pure-Edge mode keeps working.
set "VENV_PY=%APP_DIR%\scripts\venv\Scripts\python.exe"
if exist "%VENV_PY%" goto have_venv
echo [edge-tts] scripts\venv not found, falling back to system python
set "VENV_PY=python"
:have_venv
echo [edge-tts] Starting service with %VENV_PY% ...
powershell -NoProfile -Command "$env:VIBEVOICE_DISABLED='1'; Start-Process -WindowStyle Hidden -FilePath '%VENV_PY%' -ArgumentList 'scripts\edge-tts-server.py' -WorkingDirectory '%APP_DIR%' -RedirectStandardOutput '%LOG%' -RedirectStandardError '%ERRLOG%'"

REM ---- Wait for health check (up to ~20s) ----
set /a tries=0
:waitloop
%SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul
powershell -NoProfile -Command "try{ $r=Invoke-WebRequest -UseBasicParsing -Uri '%HEALTH%' -TimeoutSec 3; if($r.StatusCode -eq 200){exit 0}else{exit 1} }catch{ exit 1 }"
if !errorlevel! equ 0 (
    echo [edge-tts] Started OK: %HEALTH%
    echo [edge-tts] Log: %LOG%
    %SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul
    exit /b 0
)
set /a tries+=1
if !tries! lss 20 goto waitloop

echo [edge-tts] FAILED to start. Check error log: %ERRLOG%
pause
exit /b 1
