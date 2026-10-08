FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/ColumbiaGadgetWorks/social-media" \
      org.opencontainers.image.description="CGW Content Studio"

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

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# The entrypoint drops to PUID:PGID (default 99:100, Unraid's nobody:users) after
# making sure /data and /media are writable.
ENV PUID=99 PGID=100

EXPOSE 8080
VOLUME ["/data", "/media"]
HEALTHCHECK --interval=60s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "studio", "serve", "--host", "0.0.0.0", "--port", "8080"]
