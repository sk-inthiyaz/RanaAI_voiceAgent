#!/usr/bin/env bash
# Runs ON the Ubuntu server (22.04/24.04). Called by deploy.sh; safe to run again to update.
set -euo pipefail
APP=~/rana-voice
mkdir -p "$APP"
tar xzf ~/rana-voice.tgz -C "$APP"
chmod 600 "$APP/.env"
cd "$APP"

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  echo ">> Installing Docker (Ubuntu packages)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io docker-compose-v2 curl
  sudo systemctl enable --now docker
fi

# HTTPS name for this server. No domain needed: <ip>.sslip.io resolves to the server's IP.
if [ -z "${DOMAIN:-}" ]; then
  IP=$(curl -s https://api.ipify.org)
  DOMAIN="${IP//./-}.sslip.io"
fi
echo "DOMAIN=$DOMAIN" > deploy/.env
echo ">> Domain: https://$DOMAIN"

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 22/tcp >/dev/null; sudo ufw allow 80/tcp >/dev/null; sudo ufw allow 443/tcp >/dev/null
fi

mkdir -p deploy/logs && sudo chown -R 1000:1000 deploy/logs 2>/dev/null || true
echo ">> Building and starting (first time takes ~3-5 minutes)"
sudo docker compose -f deploy/docker-compose.yml up -d --build

echo ">> Waiting for the agent and the HTTPS certificate"
for i in $(seq 1 60); do
  if curl -fs "https://$DOMAIN/" >/dev/null 2>&1; then break; fi
  sleep 3
done
curl -s "https://$DOMAIN/" && echo
cat > ~/call.sh <<EOF
#!/usr/bin/env bash
# Place a test call:  ./call.sh 98XXXXXXXX
cd $APP && sudo docker compose -f deploy/docker-compose.yml exec -T voice python phone_server.py call "\$1"
EOF
chmod +x ~/call.sh
echo
echo "Done. Public URL: https://$DOMAIN"
echo "  Test call:        ~/call.sh 98XXXXXXXX"
echo "  Live log:         cd $APP && sudo docker compose -f deploy/docker-compose.yml logs -f voice"
echo "  Transcripts:      ls $APP/deploy/logs/transcripts/"
