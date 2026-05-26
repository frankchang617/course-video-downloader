#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=57821

if lsof -i :$PORT -sTCP:LISTEN -t &>/dev/null; then
  echo "服务已在运行，直接打开页面..."
  open "$SCRIPT_DIR/index.html"
  exit 0
fi

if ! command -v yt-dlp &>/dev/null; then
  osascript -e 'display alert "找不到 yt-dlp" message "请先在终端运行：\nbrew install yt-dlp" as critical'
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  osascript -e 'display alert "找不到 Python3" message "请先安装 Python3" as critical'
  exit 1
fi

cd "$SCRIPT_DIR"
python3 server.py &
SERVER_PID=$!

for i in {1..20}; do
  sleep 0.3
  if curl -s "http://127.0.0.1:$PORT/check" &>/dev/null; then
    break
  fi
done

open "$SCRIPT_DIR/index.html"
echo "服务 PID: $SERVER_PID  关闭此窗口即停止服务"
wait $SERVER_PID
