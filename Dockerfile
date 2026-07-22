# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS python-deps

ARG TARGETARCH
ARG CAMOUFOX_FETCH_RETRIES=8
ARG CAMOUFOX_MIN_CACHE_MB=500
ARG CAMOUFOX_RELEASE_REPO=daijro/camoufox
ARG CAMOUFOX_REPO_NAME=official
ARG CAMOUFOX_VERSION=152.0.4
ARG CAMOUFOX_BUILD=beta.28
ARG CAMOUFOX_DOWNLOAD_URL=
# Prefer official PyPI; fall back to regional mirrors when CDN returns corrupted wheels
# (pip reports that as "THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE").
ARG PIP_INDEX_URLS="https://pypi.org/simple https://pypi.tuna.tsinghua.edu.cn/simple https://mirrors.aliyun.com/pypi/simple https://mirrors.cloud.tencent.com/pypi/simple"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PYTHONPATH=/opt/python/lib/python3.12/site-packages \
    HOME=/root

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 ca-certificates \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    set -eu; \
    pip_ok=0; \
    attempt=1; \
    for index_url in ${PIP_INDEX_URLS}; do \
      host="$(printf '%s' "${index_url}" | sed -E 's#^https?://([^/]+)/.*#\1#')"; \
      echo "pip install via ${index_url}" >&2; \
      if pip install \
           --retries 5 \
           --timeout 120 \
           --index-url "${index_url}" \
           --trusted-host "${host}" \
           --prefix=/opt/python \
           -r requirements.txt; then \
        pip_ok=1; \
        break; \
      fi; \
      echo "pip install attempt ${attempt} failed on ${index_url}; trying next index" >&2; \
      attempt=$((attempt + 1)); \
      sleep $((attempt * 3)); \
    done; \
    if [ "${pip_ok}" -ne 1 ]; then \
      echo "pip install failed on all configured indexes: ${PIP_INDEX_URLS}" >&2; \
      exit 1; \
    fi; \
    rm -rf /tmp/*

RUN --mount=type=cache,target=/tmp/camoufox-download \
    set -eu; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) camoufox_arch="x86_64" ;; \
      arm64) camoufox_arch="arm64" ;; \
      386) camoufox_arch="i686" ;; \
      *) echo "Unsupported Docker target architecture for Camoufox: ${arch}" >&2; exit 1 ;; \
    esac; \
    full_version="${CAMOUFOX_VERSION}-${CAMOUFOX_BUILD}"; \
    zip_name="camoufox-${full_version}-lin.${camoufox_arch}.zip"; \
    url="${CAMOUFOX_DOWNLOAD_URL:-https://github.com/${CAMOUFOX_RELEASE_REPO}/releases/download/v${full_version}/${zip_name}}"; \
    download_dir="/tmp/camoufox-download"; \
    zip_path="${download_dir}/${zip_name}"; \
    install_root="/root/.cache/camoufox"; \
    install_path="${install_root}/browsers/${CAMOUFOX_REPO_NAME}/${full_version}"; \
    mkdir -p "${download_dir}" "${install_root}/browsers/${CAMOUFOX_REPO_NAME}"; \
    attempt=1; \
    while [ "$attempt" -le "$CAMOUFOX_FETCH_RETRIES" ]; do \
         if aria2c \
              --allow-overwrite=true \
              --auto-file-renaming=false \
              --connect-timeout=30 \
              --continue=true \
              --dir="${download_dir}" \
              --file-allocation=none \
              --max-connection-per-server=4 \
              --max-tries=20 \
              --min-split-size=16M \
              --out="${zip_name}" \
              --retry-wait=5 \
              --split=4 \
              --timeout=60 \
              "${url}" \
            && python -m zipfile -t "${zip_path}"; then \
           break; \
         fi; \
         echo "Camoufox download attempt ${attempt} did not produce a valid browser zip; retrying" >&2; \
         attempt=$((attempt + 1)); \
         if [ "$attempt" -le "$CAMOUFOX_FETCH_RETRIES" ]; then \
           sleep $((attempt * 5)); \
         fi; \
    done; \
    if ! python -m zipfile -t "${zip_path}"; then \
         echo "Camoufox browser zip was not downloaded completely: ${url}" >&2; \
         exit 1; \
    fi; \
    rm -rf "${install_path}"; \
    mkdir -p "${install_path}"; \
    python -m zipfile -e "${zip_path}" "${install_path}"; \
    install_root="${install_root}" install_path="${install_path}" full_version="${full_version}" python -c 'import json, os, pathlib; root=pathlib.Path(os.environ["install_root"]); install=pathlib.Path(os.environ["install_path"]); rel=install.relative_to(root).as_posix(); version, build = os.environ["full_version"].rsplit("-", 1); (install / "version.json").write_text(json.dumps({"version": version, "build": build, "prerelease": "beta" in build or "alpha" in build}, separators=(",", ":"))); (root / "config.json").write_text(json.dumps({"active_version": rel}, separators=(",", ":"))); (root / ".0.5_FLAG").touch()'; \
    chmod -R 755 "${install_root}"; \
    if [ "$(du -sm "${install_root}" | awk '{print $1}')" -lt "$CAMOUFOX_MIN_CACHE_MB" ]; then \
         echo "Camoufox assets were not bundled into /root/.cache/camoufox" >&2; \
         exit 1; \
    fi

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
    TURNSTILE_KEEP_BROWSER_ALIVE=1 \
    TURNSTILE_LOW_RESOURCE_MODE=0 \
    TURNSTILE_CAMOUFOX_PROFILE=compact \
    TURNSTILE_UNBLOCK_RENDERING=0 \
    TURNSTILE_WORKER_MODE=inline \
    TURNSTILE_WORKER_TIMEOUT=120 \
    TURNSTILE_SOLVE_TIMEOUT_SEC=60 \
    TURNSTILE_BROWSER_RECYCLE_TASKS=25 \
    TURNSTILE_BROWSER_RECYCLE_RSS_MB=800 \
    TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120 \
    TURNSTILE_MAX_PENDING_TASKS=4 \
    TURNSTILE_HEALTH_STUCK_SEC=180 \
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
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/*

COPY --from=python-deps /opt/python /opt/python
COPY --from=python-deps /root/.cache/camoufox /root/.cache/camoufox
COPY api_solver.py browser_configs.py db_results.py /app/
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh \
    && mkdir -p /app/logs /app/keys \
    && find /usr/local/lib -depth -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null \
    && find /usr/local/lib -name "*.pyc" -delete \
    && find /usr/local/lib -name "*.pyo" -delete \
    && rm -rf /tmp/*

EXPOSE 5072

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=8 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('TURNSTILE_PORT', '5072'), timeout=5).read()" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
