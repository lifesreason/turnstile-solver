# syntax=docker/dockerfile:1
# =============================================================================
# 1. BUILDER — 安装依赖 + 下载浏览器
# =============================================================================
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/build/browsers
RUN python -m camoufox fetch 2>&1

# Robust: 自动定位浏览器二进制并拷贝到统一位置
RUN set -eux; \
    mkdir -p /build/browsers; \
    if python -m camoufox path >/dev/null 2>&1; then \
        src="$(dirname "$(python -m camoufox path 2>/dev/null)")"; \
        [ -d "$src" ] && cp -r "$src"/* /build/browsers/ 2>/dev/null; \
    fi; \
    for d in /root/.cache/ms-playwright /root/.local/share/camoufox /root/.cache/camoufox; do \
        [ -d "$d" ] && cp -r "$d"/* /build/browsers/ 2>/dev/null || true; \
    done; \
    echo "=== Browser files ===" && ls -la /build/browsers/ || true

RUN find /usr/local/lib -depth -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
    find /usr/local/lib -name "*.pyc" -delete; \
    find /usr/local/lib -name "*.pyo" -delete

# =============================================================================
# 2. FINAL — 最小运行镜像
# =============================================================================
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/root \
    PLAYWRIGHT_BROWSERS_PATH=/app/browsers \
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

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /build/browsers /app/browsers

# 剥离 Firefox 测试/无用文件（可省 50-150MB）
RUN if [ -d /app/browsers ]; then \
        find /app/browsers -type d \( -name "test*" -o -name "tests" -o -name "mdns" -o -name "gtest" \) -exec rm -rf {} + 2>/dev/null || true; \
        find /app/browsers -name "*.txt" -path "*/test*" -delete 2>/dev/null || true; \
        find /app/browsers -name "*.json" -size -10k -delete 2>/dev/null || true; \
        find /app/browsers -name "*.dummy" -delete 2>/dev/null || true; \
    fi

COPY api_solver.py browser_configs.py db_results.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /app/logs /app/keys

EXPOSE 5072

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${TURNSTILE_PORT:-5072}/health" >/dev/null || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
