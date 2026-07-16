# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
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

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m camoufox fetch

COPY api_solver.py browser_configs.py db_results.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/logs /app/keys

# 剥离 Camoufox/Firefox 测试及无用文件
RUN if [ -d /root/.local/share/camoufox ]; then \
        find /root/.local/share/camoufox -type d \( -name "test*" -o -name "tests" -o -name "mdns" -o -name "gtest" \) -exec rm -rf {} + 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.txt" -path "*/test*" -delete 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.json" -size -10k -delete 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.dummy" -delete 2>/dev/null || true; \
    fi

RUN find /usr/local/lib -depth -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    find /usr/local/lib -name "*.pyc" -delete; \
    find /usr/local/lib -name "*.pyo" -delete

EXPOSE 5072

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${TURNSTILE_PORT:-5072}/health" >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
