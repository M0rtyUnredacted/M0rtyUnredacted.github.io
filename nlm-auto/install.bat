@echo off
:: One-time setup: install Python dependencies and Playwright browser.
:: Run this once after first cloning / downloading the repo.

echo === NLM App -- Installing dependencies ===
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ from https://python.org
    pause & exit /b 1
)

pip install -r requirements.txt
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause & exit /b 1
)

playwright install chromium
if errorlevel 1 (
    echo ERROR: playwright install failed.
    pause & exit /b 1
)

echo.
echo === Done. You can now run run.bat ===
pause
