FROM python:3.12-slim

# ffmpeg = merge video+audio, thumbnails, splitting. gcc = builds TgCrypto (fast uploads).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# JS runtime that yt-dlp needs for YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ytbot.py .

CMD ["python", "ytbot.py"]
