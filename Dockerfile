# Standalone Cloudflare Turnstile Solver（默认 amd64，构建更快）
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/root \
    TURNSTILE_HOST=0.0.0.0 \
    TURNSTILE_PORT=5072 \
    TURNSTILE_THREAD=2 \
    TURNSTILE_BROWSER_TYPE=camoufox \
    TURNSTILE_DEBUG=1 \
    TURNSTILE_LAZY=1 \
    TURNSTILE_IDLE_SEC=180 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Camoufox/Firefox 运行时依赖（精简字体）
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        xvfb \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -U pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    # 只预取 Camoufox（默认浏览器）；不装 patchright chromium，省大量时间/体积
    && python -m camoufox fetch

COPY api_solver.py browser_configs.py db_results.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/logs /app/keys

EXPOSE 5072

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${TURNSTILE_PORT:-5072}/health" >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
