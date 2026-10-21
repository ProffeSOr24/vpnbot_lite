FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip tzdata \
    && rm -rf /var/lib/apt/lists/*

# Latest Xray and Hysteria2 at build time. Later updates are installed from the bot into /data/bin.
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64) XARCH=64; HARCH=amd64 ;; \
        aarch64) XARCH=arm64-v8a; HARCH=arm64 ;; \
        *) echo "unsupported arch $(uname -m)"; exit 1 ;; \
    esac; \
    mkdir -p /opt/bin; \
    curl -fsSL -o /tmp/xray.zip "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-${XARCH}.zip"; \
    unzip -q /tmp/xray.zip xray geoip.dat geosite.dat -d /opt/bin; \
    rm /tmp/xray.zip; \
    curl -fsSL -o /opt/bin/hysteria "https://github.com/apernet/hysteria/releases/latest/download/hysteria-linux-${HARCH}"; \
    chmod +x /opt/bin/xray /opt/bin/hysteria; \
    /opt/bin/xray version; /opt/bin/hysteria version

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION .
COPY app ./app

ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
VOLUME /data
CMD ["python", "-m", "app"]
