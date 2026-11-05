@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto build
echo First run START_WINDOWS.bat, then close the application and run this file.
pause
exit /b 1
:build
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean SCUBACollector.spec
if errorlevel 1 goto failed
echo Build complete: dist\SCUBACollector\SCUBACollector.exe
echo Copy the ENTIRE dist\SCUBACollector folder to the target Windows PC.
pause
exit /b 0
:failed
echo Build failed. See errors above.
pause
exit /b 1
