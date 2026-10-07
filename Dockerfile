FROM python:3.12-slim

# ffmpeg: video probing and key frames. tini: clean signal handling.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY studio ./studio
COPY guidelines ./guidelines

ENV STUDIO_DATA_DIR=/data \
    STUDIO_MEDIA_DIR=/media \
    PYTHONUNBUFFERED=1

# Unraid's default owner is nobody:users (99:100), so files on the array stay readable.
RUN mkdir -p /data /media && chown 99:100 /data /media
USER 99:100

EXPOSE 8080
VOLUME ["/data", "/media"]
HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "studio", "serve", "--host", "0.0.0.0", "--port", "8080"]
