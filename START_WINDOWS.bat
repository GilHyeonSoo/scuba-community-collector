@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto install
where py >nul 2>&1
if errorlevel 1 goto use_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto missing
py -3 -m venv .venv
if errorlevel 1 goto failed
goto install
:use_python
where python >nul 2>&1
if errorlevel 1 goto missing
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto missing
python -m venv .venv
if errorlevel 1 goto failed
:install
if not exist ".venv\requirements.installed" goto pip_install
fc /b requirements.txt ".venv\requirements.installed" >nul 2>&1
if not errorlevel 1 goto launch
:pip_install
echo Installing required packages. Internet is needed on first start.
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
copy /y requirements.txt ".venv\requirements.installed" >nul
:launch
".venv\Scripts\python.exe" -m collector %*
if errorlevel 1 goto failed
exit /b 0
:missing
echo Install 64-bit Python 3.11 or 3.12 from https://www.python.org/downloads/windows/
echo Select "Add python.exe to PATH", then run this file again.
pause
exit /b 1
:failed
echo Startup failed. Check the error above and README.md.
pause
exit /b 1
