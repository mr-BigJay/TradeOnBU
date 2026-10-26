FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends chromium xvfb x11vnc novnc websockify tini ca-certificates fonts-noto-core fonts-noto-extra && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.51.0 && useradd --create-home --uid 1000 reader && mkdir /state /export && chown reader:reader /state /export
WORKDIR /reader
COPY reader.py ./
COPY start.sh ./
USER reader
ENV DISPLAY=:99 HOME=/home/reader PYTHONUNBUFFERED=1
ENTRYPOINT ["/usr/bin/tini", "--", "/bin/sh", "/reader/start.sh"]
