@echo off
setlocal EnableDelayedExpansion

set APP_DIR=C:\nlm_app
set CHROME="C:\Program Files\Google\Chrome\Application\chrome.exe"

echo === NLM Automation App ===
echo.

:: -----------------------------------------------------------------
:: 1. Make sure the app directory exists and go there
:: -----------------------------------------------------------------
if not exist "%APP_DIR%" (
    echo Creating %APP_DIR% ...
    mkdir "%APP_DIR%"
)
cd /d "%APP_DIR%"

:: -----------------------------------------------------------------
:: 2. Seed config.json on first run
:: -----------------------------------------------------------------
if not exist "config.json" (
    copy config_template.json config.json >nul
    echo.
    echo ================================================================
    echo  FIRST-TIME SETUP - fill in config.json before the app can run.
    echo  Opening it now in Notepad ...
    echo ================================================================
    echo.
    notepad config.json
    echo After saving config.json, also copy credentials.json (service
    echo account) to %APP_DIR%\ then run run.bat again.
    pause
    exit /b 0
)

:: -----------------------------------------------------------------
:: 3. Check credentials.json
:: -----------------------------------------------------------------
if not exist "credentials.json" (
    echo ERROR: credentials.json not found in %APP_DIR%\
    echo Copy your Google service-account JSON file there and try again.
    pause & exit /b 1
)

:: -----------------------------------------------------------------
:: 4. Install Python dependencies (if not already done)
:: -----------------------------------------------------------------
if not exist "%APP_DIR%\.deps_installed" (
    echo Installing dependencies -- this only runs once ...
    pip install -r requirements.txt --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo ERROR: pip install failed. Is Python 3 installed and on PATH?
        pause & exit /b 1
    )
    playwright install chromium --quiet 2>nul
    echo installed > "%APP_DIR%\.deps_installed"
    echo Dependencies installed.
) else (
    echo Dependencies already installed.
)

:: -----------------------------------------------------------------
:: 5. Chrome remote-debugging session
::
:: If port 9222 is already open, reuse it.
:: If not, kill any stale Chrome processes then launch fresh with
:: --remote-debugging-port=9222 using the profile from config.json.
::
:: IMPORTANT: uses 127.0.0.1 -- on Windows, 'localhost' may resolve
:: to IPv6 (::1) but Chrome binds on IPv4 only.
:: -----------------------------------------------------------------

:: Read profile path from config.json
for /f "delims=" %%P in ('python -c "import json,os; c=json.load(open('config.json')); p=c.get('notebooklm',{}).get('chrome_profile_path',''); ud=os.path.dirname(p) if p else ''; pd=os.path.basename(p) if p else 'Default'; print(ud+'|'+pd)" 2^>nul') do set CHROME_INFO=%%P
for /f "tokens=1 delims=|" %%A in ("%CHROME_INFO%") do set CHROME_USER_DATA=%%A
for /f "tokens=2 delims=|" %%B in ("%CHROME_INFO%") do set CHROME_PROFILE=%%B
if "%CHROME_USER_DATA%"=="" set CHROME_USER_DATA=%LOCALAPPDATA%\Google\Chrome\User Data
if "%CHROME_PROFILE%"=="" set CHROME_PROFILE=Default

echo Checking Chrome debug port 9222 ...
powershell -NoProfile -Command ^
    "try { $r=(Invoke-WebRequest 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 2).StatusCode; exit ($r -ne 200) } catch { exit 1 }" >nul 2>&1
if errorlevel 1 (
    echo Port 9222 not responding -- launching Chrome ...
    echo   User data: %CHROME_USER_DATA%
    echo   Profile  : %CHROME_PROFILE%

    :: Kill any Chrome that is already running without a debug port
    :: (it would block a new launch from binding port 9222)
    taskkill /f /im chrome.exe >nul 2>&1
    timeout /t 2 /nobreak >nul

    if not exist %CHROME% (
        echo ERROR: Chrome not found at %CHROME%
        echo Install Chrome or update the CHROME variable in run.bat.
        pause & exit /b 1
    )

    start "" %CHROME% ^
        --remote-debugging-port=9222 ^
        --user-data-dir="%CHROME_USER_DATA%" ^
        --profile-directory="%CHROME_PROFILE%" ^
        --no-first-run ^
        --no-default-browser-check

    :: Wait up to 15s for Chrome to bind the port
    echo Waiting for Chrome to bind port 9222 ...
    set /a tries=0
    :wait_loop
    timeout /t 2 /nobreak >nul
    set /a tries+=1
    powershell -NoProfile -Command ^
        "try { $r=(Invoke-WebRequest 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 1).StatusCode; exit ($r -ne 200) } catch { exit 1 }" >nul 2>&1
    if not errorlevel 1 goto chrome_ready
    if !tries! lss 7 goto wait_loop
    echo ERROR: Chrome did not bind port 9222 after 14 seconds.
    echo   Try running this manually to diagnose:
    echo   curl http://127.0.0.1:9222/json/version
    echo   Also check: dir "%LOCALAPPDATA%\Google\Chrome\User Data\" /b /ad
    pause & exit /b 1
) else (
    echo Chrome already on port 9222 -- reusing session.
)
:chrome_ready

:: -----------------------------------------------------------------
:: 6. Launch app
:: -----------------------------------------------------------------
echo.
echo Starting NLM Automation App ...
echo Gradio UI -> http://localhost:7860
echo.
python main.py

pause
