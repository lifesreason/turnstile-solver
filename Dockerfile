# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS python-deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONPATH=/opt/python/lib/python3.12/site-packages \
    HOME=/root

WORKDIR /build

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --prefix=/opt/python -r requirements.txt \
    && python -m camoufox fetch \
    && if [ -d /root/.local/share/camoufox ]; then \
        find /root/.local/share/camoufox -type d \( -name "test*" -o -name "tests" -o -name "mdns" -o -name "gtest" \) -exec rm -rf {} + 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.txt" -path "*/test*" -delete 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.json" -size -10k -delete 2>/dev/null || true; \
        find /root/.local/share/camoufox -name "*.dummy" -delete 2>/dev/null || true; \
    fi \
    && find /opt/python -depth -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null \
    && find /opt/python -name "*.pyc" -delete \
    && find /opt/python -name "*.pyo" -delete \
    && rm -rf /root/.cache /tmp/*

FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/python/lib/python3.12/site-packages \
    PATH=/opt/python/bin:$PATH \
    HOME=/root \
    TURNSTILE_HOST=0.0.0.0 \
    TURNSTILE_PORT=5072 \
    TURNSTILE_THREAD=1 \
    TURNSTILE_BROWSER_INSTANCES=1 \
    TURNSTILE_BROWSER_TYPE=camoufox \
    TURNSTILE_DEBUG=0 \
    TURNSTILE_LAZY=1 \
    TURNSTILE_IDLE_SEC=60 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
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
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /root/.cache /tmp/*

COPY --from=python-deps /opt/python /opt/python
COPY --from=python-deps /root/.local/share/camoufox /root/.local/share/camoufox
COPY api_solver.py browser_configs.py db_results.py /app/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/logs /app/keys \
    && find /usr/local/lib -depth -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null \
    && find /usr/local/lib -name "*.pyc" -delete \
    && find /usr/local/lib -name "*.pyo" -delete \
    && rm -rf /root/.cache /tmp/*

EXPOSE 5072

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=8 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('TURNSTILE_PORT', '5072'), timeout=5).read()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
