#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "正在開啟記帳工具…"
exec python3 "系統檔案/backup_server.py" --launch
