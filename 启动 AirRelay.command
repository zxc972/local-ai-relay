#!/bin/sh
# macOS: double-click to start AirRelay and open it in the browser.
case "$0" in */*) cd "${0%/*}" || exit 1 ;; esac
for candidate in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3.13 python3.12 python3.11 python3.10; do
  if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "AirRelay 正在运行。关闭这个窗口即停止。"
    exec "$candidate" local_dev.py serve --open
  fi
done
echo "需要 Python 3.10 或更高版本，请先安装：https://www.python.org/downloads/"
echo "按回车键关闭。"
read -r _
