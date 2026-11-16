# RANA R3 voice agent — phone server (Vobiz <-> Pipecat <-> Sarvam).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 TZ=Asia/Kolkata
RUN apt-get update && apt-get install -y --no-install-recommends libsndfile1 curl tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --retries 10 --timeout 60 -r requirements.txt

COPY bot.py phone_server.py sarvam_stream_tts.py call_log.py winloop.py agent_prompt.txt ./

# Run as a normal user; logs/ is a mounted volume.
RUN useradd -m rana && mkdir -p /app/logs && chown -R rana /app
USER rana

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s CMD curl -fs http://localhost:8765/ || exit 1
CMD ["python", "phone_server.py"]
