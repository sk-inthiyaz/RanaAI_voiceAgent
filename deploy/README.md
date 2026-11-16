# Put RANA R3 on a server

The same agent you tested on the laptop, on a Linux server in India, running 24/7 with HTTPS.
No domain needed (uses `<server-ip>.sslip.io`), no Cloudflare tunnel.

## 1. Get a server (one time, ~10 minutes)

Any Ubuntu 22.04 / 24.04 server in **Mumbai or Bangalore**, **2 vCPU, 4 GB RAM**, with a public IP:

| Provider | Plan | About |
|---|---|---|
| AWS Lightsail (Mumbai) | 4 GB | ₹2,000/month |
| DigitalOcean (Bangalore) | Basic 4 GB | ₹2,000/month |
| E2E Networks (Delhi/Mumbai, Indian) | C3 2 vCPU / 4 GB | ₹1,500–2,500/month |

When creating it: choose **Ubuntu**, download the **SSH key** (.pem), and open ports **22, 80, 443**
(Lightsail: Networking → IPv4 firewall → add HTTP and HTTPS).

## 2. Deploy (from this laptop, Git Bash, in the `voice-agent` folder)

```
bash deploy/deploy.sh ubuntu@SERVER_IP path/to/key.pem
```

It copies the agent and your `.env`, installs Docker, starts the agent + HTTPS, and prints the public URL.
First run takes 3–5 minutes. Run the same command again after any change to update the server.

## 3. Test call

```
ssh -i path/to/key.pem ubuntu@SERVER_IP
./call.sh 98XXXXXXXX
```

## Logs

| What | Where (on the server) |
|---|---|
| Live log while calling | `cd ~/rana-voice && sudo docker compose -f deploy/docker-compose.yml logs -f voice` |
| One readable transcript per call (with delays, cost) | `~/rana-voice/deploy/logs/transcripts/<date>/` |
| One line per call (JSON) | `~/rana-voice/deploy/logs/calls.jsonl` |
| Last 20 calls in the browser | `https://<domain>/calls/<CALL_SECRET>` |
| Everything (debug) / only problems | `deploy/logs/phone_<date>.log` / `deploy/logs/errors_<date>.log` |
| Copy all logs to the laptop | `bash deploy/get_logs.sh ubuntu@SERVER_IP key.pem` → `server_logs/` |

Health check: `https://<domain>/` shows the models, settings, uptime and active calls.
