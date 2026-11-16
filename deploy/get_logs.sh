#!/usr/bin/env bash
# Copy the server's call records + transcripts + logs to this laptop (into server_logs/):
#   bash deploy/get_logs.sh ubuntu@SERVER_IP [key.pem]
set -euo pipefail
HOST="${1:?Usage: bash deploy/get_logs.sh ubuntu@SERVER_IP [key.pem]}"
OPTS=(-o StrictHostKeyChecking=accept-new); [ -n "${2:-}" ] && OPTS+=(-i "$2")
cd "$(dirname "$0")/.."
mkdir -p server_logs
scp -r "${OPTS[@]}" "$HOST:~/rana-voice/deploy/logs/*" server_logs/
echo "Saved to server_logs/ (calls.jsonl, transcripts/, phone_*.log, errors_*.log)"
