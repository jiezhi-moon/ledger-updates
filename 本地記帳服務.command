#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
echo "這是記帳服務視窗，關閉此視窗或按 Control+C 可停止服務。"
exec python3 "系統檔案/backup_server.py" --serve
