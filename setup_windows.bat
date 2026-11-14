@echo off
cd /d "%~dp0"
echo STEP 1: Creating Python virtual environment...
py -3.11 -m venv .venv
if errorlevel 1 python -m venv .venv
if errorlevel 1 (
  echo ERROR: Install Python 3.11 or newer and retry.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo STEP 2: Installing required packages...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo ERROR: Python package installation failed.
  pause
  exit /b 1
)
echo STEP 3: Creating local accounts and sample events...
python seed.py --sample-data
if errorlevel 1 (
  echo ERROR: Initial data setup failed.
  pause
  exit /b 1
)
echo.
echo Setup successful. Double-click run_windows.bat to start EventSphere.
pause
