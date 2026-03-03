@echo off
setlocal EnableDelayedExpansion

set APP_DIR=C:\nlm_app
set REPO=https://github.com/M0rtyUnredacted/nlm-auto.git
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"

:: ── 1. Create app directory ─────────────────────────────────────────────────
if not exist "%APP_DIR%" (
    echo Creating %APP_DIR% ...
    mkdir "%APP_DIR%"
)

:: ── 2. Clone or pull latest code ─────────────────────────────────────────────
if not exist "%APP_DIR%\.git" (
    echo Cloning repo ...
    git clone %REPO% "%APP_DIR%"
    if errorlevel 1 (
        echo ERROR: git clone failed. Is git installed?
        pause & exit /b 1
    )
) else (
    echo Pulling latest code ...
    git -C "%APP_DIR%" pull origin main --quiet
)

cd /d "%APP_DIR%"

:: ── 3. Seed config.json on first run ─────────────────────────────────────────
if not exist "config.json" (
    copy config_template.json config.json >nul
    echo.
    echo ================================================================
    echo  FIRST-TIME SETUP — fill in config.json before the app can run.
    echo  Opening it now in Notepad ...
    echo ================================================================
    echo.
    notepad config.json
    echo After saving config.json, also copy credentials.json ^(service
    echo account^) to %APP_DIR%\ then run run.bat again.
    pause
    exit /b 0
)

:: ── 4. Check credentials.json ────────────────────────────────────────────────
if not exist "credentials.json" (
    echo ERROR: credentials.json not found in %APP_DIR%\
    echo Copy your Google service-account JSON file there and try again.
    pause & exit /b 1
)

:: ── 5. Install / update Python dependencies ──────────────────────────────────
echo Installing dependencies ...
pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo ERROR: pip install failed. Is Python installed and on PATH?
    pause & exit /b 1
)
playwright install chromium --quiet 2>nul

:: ── 6. Chrome remote-debugging session ───────────────────────────────────────
:: Read chrome_profile_path from config.json via Python (falls back to default)
for /f "delims=" %%P in ('python -c "import json,os; c=json.load(open('config.json')); p=c.get('notebooklm',{}).get('chrome_profile_path',''); ud=os.path.dirname(p) if p else ''; pd=os.path.basename(p) if p else 'Default'; print(ud+'|'+pd)" 2^>nul') do set CHROME_INFO=%%P
for /f "tokens=1 delims=|" %%A in ("%CHROME_INFO%") do set CHROME_USER_DATA=%%A
for /f "tokens=2 delims=|" %%B in ("%CHROME_INFO%") do set CHROME_PROFILE=%%B
if "%CHROME_USER_DATA%"=="" set CHROME_USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data
if "%CHROME_PROFILE%"=="" set CHROME_PROFILE=Default

netstat -ano | findstr ":9222" >nul 2>&1
if errorlevel 1 (
    echo Starting Chrome with remote-debugging on port 9222 ...
    echo   Profile: %CHROME_USER_DATA%\%CHROME_PROFILE%
    if not exist %CHROME% (
        echo WARNING: Chrome not found at default path.
        echo          Start Chrome manually with --remote-debugging-port=9222
    ) else (
        start "" %CHROME% --remote-debugging-port=9222 ^
            --user-data-dir="%CHROME_USER_DATA%" ^
            --profile-directory="%CHROME_PROFILE%"
        timeout /t 3 /nobreak >nul
    )
) else (
    echo Chrome debug port 9222 already open — reusing session.
)

:: ── 7. Launch app ────────────────────────────────────────────────────────────
echo.
echo Starting NLM Automation App ...
echo Gradio UI -> http://localhost:7860
echo.
python main.py

pause
