#!/usr/bin/env bash
# Run on your laptop (Git Bash), from the voice-agent folder:
#   bash deploy/deploy.sh ubuntu@SERVER_IP [path/to/key.pem]
# Copies the agent + your .env to the server and starts it there (Docker + automatic HTTPS). Run again to update.
set -euo pipefail
HOST="${1:?Usage: bash deploy/deploy.sh ubuntu@SERVER_IP [key.pem]}"
KEY="${2:-}"
OPTS=(-o StrictHostKeyChecking=accept-new)
[ -n "$KEY" ] && OPTS+=(-i "$KEY")
cd "$(dirname "$0")/.."

[ -f .env ] || { echo "No .env here — run from the voice-agent folder."; exit 1; }
echo ">> Packing"
tar czf "${TMPDIR:-/tmp}/rana-voice.tgz" bot.py phone_server.py sarvam_stream_tts.py call_log.py winloop.py \
  agent_prompt.txt requirements.txt Dockerfile .dockerignore .env deploy/docker-compose.yml deploy/Caddyfile \
  deploy/server_setup.sh
echo ">> Uploading to $HOST"
scp "${OPTS[@]}" "${TMPDIR:-/tmp}/rana-voice.tgz" "$HOST:~/rana-voice.tgz"
echo ">> Setting up on the server"
ssh "${OPTS[@]}" "$HOST" "DOMAIN='${DOMAIN:-}' bash -s" < deploy/server_setup.sh
