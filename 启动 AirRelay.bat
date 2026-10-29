@echo off
rem Windows: double-click to start AirRelay and open it in the browser.
chcp 65001 >nul
cd /d "%~dp0"
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
if not defined PY (
  echo 需要 Python 3.10 或更高版本，请先安装：https://www.python.org/downloads/
  pause
  exit /b 1
)
echo AirRelay 正在运行。关闭这个窗口即停止。
%PY% local_dev.py serve --open
pause
