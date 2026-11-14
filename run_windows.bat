@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\activate.bat" (
  echo Run setup_windows.bat first.
  pause
  exit /b 1
)
call ".venv\Scripts\activate.bat"
echo.
echo EventSphere website: http://127.0.0.1:8000
echo Stop by pressing CTRL+C in this window.
echo.
python -m uvicorn main:app --reload
pause
