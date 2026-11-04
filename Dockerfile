FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    DISPLAY=:1 \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        x11vnc \
        x11-utils \
        xauth \
        scrot \
        gnome-screenshot \
        fluxbox \
        novnc \
        websockify \
        chromium \
        python3 \
        python3-pip \
        python3-tk \
        python3-dev \
        python3-xlib \
        supervisor \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && touch /root/.Xauthority

COPY stack/requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

RUN mkdir -p /app /app/logs /app/observer
WORKDIR /app

COPY supervisord.conf /etc/supervisor/supervisord.conf
COPY wait-for-display.sh /usr/local/bin/wait-for-display.sh
RUN chmod +x /usr/local/bin/wait-for-display.sh

EXPOSE 3000 5051 8888

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
