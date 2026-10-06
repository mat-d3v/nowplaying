# nowplaying: the bridge and its pages, on the standard library only
#   docker run -d --network host -v nowplaying-data:/data ghcr.io/mat-d3v/nowplaying
FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/mat-d3v/nowplaying" \
      org.opencontainers.image.description="Fullscreen Now Playing page for mpd, AirPlay and Spotify Connect" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app
COPY mpd-bridge.py spotify-event.py index.html settings.html history.html hires.svg manifest.webmanifest ./
COPY nowplaying/*.py nowplaying/
RUN python3 -m compileall -q nowplaying
COPY assets/icon-192.png assets/logo.png assets/apple-touch-icon.png assets/touch-icon-v2.png assets/

# Not as root; what it saves (display settings, listening history) in /data
RUN adduser -S -D -H -u 10001 nowplaying \
    && mkdir /data && chown nowplaying /data
USER nowplaying
# No Bluetooth: the container can't reach the machine's BlueZ
ENV DATA_DIR=/data BLUETOOTH=0
VOLUME /data

EXPOSE 8766
CMD ["python3", "mpd-bridge.py"]
