# turnstile-solver

`turnstile-solver` 是一个可独立部署的 Cloudflare Turnstile 求解服务。本仓库把浏览器求解逻辑封装为 HTTP API，供其它业务服务通过网络调用，避免每个业务项目都内置一套浏览器和验证码处理流程。

当前仓库重点面向 Docker / NAS / 服务器部署，并针对运行内存做了低资源默认配置：

- 镜像内置 Camoufox 浏览器资产，容器启动不依赖运行时下载。
- 服务启动后不立即启动浏览器，第一次收到解题任务时才懒加载。
- 默认只启动 1 个真实浏览器进程。
- 默认任务结束后立即关闭浏览器进程，降低调用后的常驻内存。
- 默认每个解题任务由独立 worker 进程执行，浏览器内存不留在主 HTTP 服务进程里。
- 默认持续阻断图片、字体、样式等重页面资源，减少单次解题峰值。
- `/health` 提供浏览器池、进程 RSS 和浏览器子进程诊断信息。

> 请只在你有授权的业务流程中使用本服务。Turnstile token 与访问页面、站点 key、出口 IP、会话状态等因素有关，生成后也有时效限制。

## 工作方式

服务启动后只运行一个轻量 HTTP 进程。业务调用 `createTask` 或 `/turnstile` 后，主服务默认会启动一个短生命周期 worker 子进程，由 worker 启动 Camoufox 浏览器、打开目标页面、注入/定位 Turnstile 组件并等待 token。worker 将结果回传给主服务后退出，浏览器峰值内存被限制在 worker 生命周期内。

如果设置 `TURNSTILE_WORKER_MODE=inline`，主服务会在自身进程里执行浏览器求解。这主要用于调试，不建议在低内存部署中使用。

Docker 镜像在构建阶段下载 Camoufox release zip，校验 zip 完整性，解压到 `/root/.cache/camoufox`，再把该目录复制进最终镜像。构建阶段使用可断点续传的下载方式，避免 GitHub 下载中断后只留下空缓存。容器启动时只做同样的完整性校验，不会再下载浏览器；如果资产缺失会直接退出并提示重新构建镜像。

## 接口

本服务提供两套接口：

| 接口 | 方法 | 用途 |
|------|------|------|
| `/createTask` | `POST` | YesCaptcha / CapSolver 兼容创建任务 |
| `/getTaskResult` | `POST` | YesCaptcha / CapSolver 兼容轮询结果 |
| `/turnstile` | `GET` | 原生创建任务 |
| `/result` | `GET` | 原生轮询结果 |
| `/health` | `GET` | 健康检查、配置和内存诊断 |
| `/reclaim` | `GET` / `POST` | 手动回收浏览器池 |

支持的兼容任务类型：

- `TurnstileTaskProxyless`
- `TurnstileTaskProxylessM1`
- `TurnstileTaskProxylessM2`
- `TurnstileTask`
- `AntiTurnstileTaskProxyLess`
- `AntiTurnstileTaskProxyless`
- `AntiTurnstileTask`

这些类型在本地服务里走同一条 Turnstile 求解路径。

## 快速部署

### 1. 准备目录

```bash
mkdir -p ~/turnstile-solver
cd ~/turnstile-solver
```

复制本仓库的 `docker-compose.yml` 到该目录。也可以从 GitHub raw 地址下载：

```bash
curl -fsSL -o docker-compose.yml \
  https://raw.githubusercontent.com/jeck5001/turnstile-solver/master/docker-compose.yml
```

### 2. 启动服务

```bash
docker compose pull
docker compose up -d
```

Camoufox 浏览器资产已经打包在镜像内，正常情况下容器启动时不会再下载浏览器。

### 3. 检查状态

```bash
curl -s http://127.0.0.1:5072/health
```

典型返回：

```json
{
  "ok": true,
  "browser_type": "camoufox",
  "concurrency_slots": 1,
  "browser_instances": 1,
  "keep_browser_alive": false,
  "pool_ready": false,
  "process_rss_mb": 100.0,
  "children_rss_mb": 0.0,
  "browser_process_rss_mb": 0.0,
  "browser_processes": []
}
```

如果 GHCR 包是 private，需要先登录：

```bash
echo <GITHUB_TOKEN> | docker login ghcr.io -u <GITHUB_USERNAME> --password-stdin
```

## 调用示例

### YesCaptcha / CapSolver 兼容协议

创建任务：

```bash
curl -s http://127.0.0.1:5072/createTask \
  -H 'Content-Type: application/json' \
  -d '{
    "clientKey": "local",
    "task": {
      "type": "TurnstileTaskProxyless",
      "websiteURL": "https://example.com",
      "websiteKey": "0x4AAAA...",
      "proxy": "http://127.0.0.1:7890"
    }
  }'
```

返回：

```json
{
  "errorId": 0,
  "taskId": "..."
}
```

轮询结果：

```bash
curl -s http://127.0.0.1:5072/getTaskResult \
  -H 'Content-Type: application/json' \
  -d '{
    "clientKey": "local",
    "taskId": "<上一步 taskId>"
  }'
```

处理中：

```json
{
  "errorId": 0,
  "status": "processing",
  "taskId": "..."
}
```

成功：

```json
{
  "errorId": 0,
  "status": "ready",
  "taskId": "...",
  "solution": {
    "token": "..."
  }
}
```

失败：

```json
{
  "errorId": 1,
  "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
  "errorDescription": "Workers could not solve the Captcha"
}
```

### 原生 GET 协议

创建任务：

```bash
curl -s 'http://127.0.0.1:5072/turnstile?url=https://example.com&sitekey=0x4AAAA...'
```

轮询结果：

```bash
curl -s 'http://127.0.0.1:5072/result?id=<taskId>'
```

原生协议也支持任务级代理：

```bash
curl -s 'http://127.0.0.1:5072/turnstile?url=https://example.com&sitekey=0x4AAAA...&proxy=http://user:pass@host:port'
```

## 代理设置

代理优先级如下：

1. 任务级代理：`task.proxy`、`task.proxyUrl`、`task.proxyURL`
2. 分字段代理：`proxyAddress` + `proxyPort` + 可选 `proxyLogin` / `proxyPassword`
3. 进程级代理池：启动时开启 `TURNSTILE_PROXY=1`，并在工作目录放置 `proxies.txt`
4. 不使用代理

`proxies.txt` 示例：

```text
http://user:pass@1.2.3.4:8080
socks5://127.0.0.1:7890
```

实际业务中，Turnstile token 通常和访问出口相关。调用方访问目标站点的出口 IP 与 solver 解题使用的出口 IP 越一致，token 被接受的概率越高。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `TURNSTILE_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `TURNSTILE_PORT` | `5072` | HTTP 监听端口 |
| `TURNSTILE_THREAD` | `1` | 并发解题槽位数，不等同于真实浏览器进程数 |
| `TURNSTILE_BROWSER_INSTANCES` | `1` | 真实浏览器进程数，最大不会超过 `TURNSTILE_THREAD` |
| `TURNSTILE_BROWSER_TYPE` | `camoufox` | `camoufox` / `chromium` / `chrome` / `msedge` |
| `TURNSTILE_DEBUG` | `0` | `1` 开启详细日志 |
| `TURNSTILE_LAZY` | `1` | `1` 表示首次任务再启动浏览器 |
| `TURNSTILE_KEEP_BROWSER_ALIVE` | `0` | `0` 表示任务完成后立即关闭浏览器；`1` 表示保温浏览器池 |
| `TURNSTILE_UNBLOCK_RENDERING` | `0` | `0` 表示持续阻断图片/字体/样式等重资源；兼容性问题可设 `1` |
| `TURNSTILE_WORKER_MODE` | `process` | `process` 表示每个任务使用独立 worker 子进程；`inline` 表示在主服务进程内求解 |
| `TURNSTILE_WORKER_TIMEOUT` | `120` | worker 单任务超时秒数，超时后主服务会终止 worker 进程树 |
| `TURNSTILE_IDLE_SEC` | `60` | 保温模式下的空闲回收秒数；仅 `TURNSTILE_KEEP_BROWSER_ALIVE=1` 时有意义 |
| `TURNSTILE_PROXY` | `0` | `1` 表示启用 `proxies.txt` 代理池 |
| `TURNSTILE_SHM_SIZE` | `512mb` | Docker `/dev/shm` 大小；复杂页面或更高并发可设 `1gb` / `2gb` |
| `CAMOUFOX_MIN_CACHE_MB` | `500` | 容器启动时校验内置 Camoufox 资产的最小体积 |
| `API_KEY` | 空 | 设置后请求必须带相同 `clientKey` |

## 资源模式

### 低内存默认模式

默认配置适合 NAS 或小内存服务器：

```env
TURNSTILE_THREAD=1
TURNSTILE_BROWSER_INSTANCES=1
TURNSTILE_KEEP_BROWSER_ALIVE=0
TURNSTILE_UNBLOCK_RENDERING=0
TURNSTILE_WORKER_MODE=process
TURNSTILE_WORKER_TIMEOUT=120
TURNSTILE_SHM_SIZE=512mb
```

特点：

- 启动后常驻内存较低。
- 每次任务会启动独立 worker，worker 内部启动浏览器，任务结束后 worker 退出。
- 延迟比保温模式更高，但调用结束后不应长期保留浏览器进程。

### 低延迟保温模式

如果调用频繁，可以保留浏览器池：

```env
TURNSTILE_KEEP_BROWSER_ALIVE=1
TURNSTILE_IDLE_SEC=180
```

特点：

- 减少每次任务的浏览器冷启动时间。
- 空闲期间浏览器仍会占用内存。
- 到达 `TURNSTILE_IDLE_SEC` 后自动回收。

### 更高并发

优先只增加并发槽位：

```env
TURNSTILE_THREAD=2
TURNSTILE_BROWSER_INSTANCES=1
```

只有确实需要多个独立浏览器进程时，再增加：

```env
TURNSTILE_BROWSER_INSTANCES=2
TURNSTILE_SHM_SIZE=1gb
```

## 内存排查

调用后如果 Docker 面板显示内存较高，先看服务自己的诊断：

```bash
curl -s http://127.0.0.1:5072/health
```

重点字段：

| 字段 | 含义 |
|------|------|
| `process_rss_mb` | Python 主服务进程 RSS |
| `children_rss_mb` | 当前服务子进程 RSS 总和 |
| `browser_process_rss_mb` | 浏览器相关子进程 RSS 总和 |
| `browser_processes` | 浏览器相关子进程列表 |
| `pool_ready` | 浏览器池是否仍处于可用状态 |
| `owned` | 当前服务持有的真实浏览器实例数量 |
| `queue` | 当前可用并发槽位数量 |
| `in_flight` | 当前正在处理的任务数量 |

判断方式：

- `browser_process_rss_mb > 0`：还有浏览器进程在运行，可能是任务未结束、保温模式开启，或浏览器残留未清理。
- `browser_process_rss_mb = 0` 但 Docker 面板仍显示高：更可能是容器 page cache 或面板统计口径，不是活跃浏览器进程 RSS。
- `pool_ready = true` 且 `owned > 0`：浏览器池仍保温；确认是否设置了 `TURNSTILE_KEEP_BROWSER_ALIVE=1`。

手动回收：

```bash
curl -s -X POST http://127.0.0.1:5072/reclaim
```

查看容器日志：

```bash
docker logs -f turnstile-solver
```

查看 Docker 实时统计：

```bash
docker stats turnstile-solver
```

## 更新部署

拉取新镜像并强制重建容器：

```bash
docker compose pull
docker compose up -d --force-recreate
```

如果你从旧版迁移，建议确认 compose 展开后的关键值：

```bash
docker compose config
```

应看到类似：

```yaml
TURNSTILE_KEEP_BROWSER_ALIVE: "0"
TURNSTILE_UNBLOCK_RENDERING: "0"
TURNSTILE_WORKER_MODE: "process"
TURNSTILE_WORKER_TIMEOUT: "120"
TURNSTILE_BROWSER_INSTANCES: "1"
TURNSTILE_THREAD: "1"
shm_size: "536870912"
```

## 本地开发

本地脚本会创建 `.venv`、安装依赖并下载浏览器资产：

```bash
./start.sh
```

查看日志：

```bash
tail -f logs/turnstile_solver.log
```

停止：

```bash
./stop.sh
```

也可以手动运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip setuptools wheel
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m camoufox fetch
.venv/bin/python api_solver.py --browser_type camoufox --thread 1 --host 0.0.0.0 --port 5072
```

运行检查：

```bash
python3 -m unittest discover
PYTHONPYCACHEPREFIX=/private/tmp/turnstile-solver-pycache python3 -m py_compile api_solver.py browser_configs.py db_results.py
docker compose config
bash -n entrypoint.sh
bash -n start.sh
git diff --check
```

## GitHub Actions 构建

仓库包含 `.github/workflows/docker-image.yml`，推送到 `master` 或 `main` 时会构建并推送 GHCR 镜像：

```text
ghcr.io/<owner>/<repo>:latest
ghcr.io/<owner>/<repo>:<branch>
ghcr.io/<owner>/<repo>:sha-<commit>
```

手动触发 workflow 时可以指定平台，默认是：

```text
linux/amd64
```

Dockerfile 提供构建参数，用于控制 Camoufox 打包阶段：

| 构建参数 | 默认 | 说明 |
|------|------|------|
| `CAMOUFOX_FETCH_RETRIES` | `8` | 构建阶段下载 Camoufox 资产的重试次数 |
| `CAMOUFOX_MIN_CACHE_MB` | `500` | 构建阶段判定 Camoufox 资产已完整打包的最小体积 |
| `CAMOUFOX_RELEASE_REPO` | `daijro/camoufox` | Camoufox release 所在 GitHub 仓库 |
| `CAMOUFOX_REPO_NAME` | `official` | 写入 Camoufox 缓存路径时使用的仓库名 |
| `CAMOUFOX_VERSION` | `152.0.4` | Camoufox 浏览器版本 |
| `CAMOUFOX_BUILD` | `beta.28` | Camoufox 浏览器构建号 |
| `CAMOUFOX_DOWNLOAD_URL` | 空 | 指定后使用该 URL 下载浏览器 zip，适合内部镜像源或缓存源 |

本地手动构建：

```bash
docker build \
  --build-arg CAMOUFOX_FETCH_RETRIES=8 \
  --build-arg CAMOUFOX_MIN_CACHE_MB=500 \
  -t turnstile-solver:local .
```

如果构建日志出现 `Camoufox assets were not bundled into /root/.cache/camoufox`，说明构建阶段没有完整下载 Camoufox 浏览器包，需要重新触发构建或临时调高 `CAMOUFOX_FETCH_RETRIES`。

如果 GitHub release 下载在你的 CI 环境里不稳定，推荐把 `CAMOUFOX_DOWNLOAD_URL` 指向你自己的对象存储或制品缓存中的同名 zip。Dockerfile 会继续做 zip 完整性校验和缓存体积校验。

## 目录结构

```text
api_solver.py              HTTP 服务、解题流程、浏览器池和内存诊断
browser_configs.py         Chromium / Chrome / Edge UA 与 sec-ch-ua 配置池
db_results.py              内存任务结果存储与过期清理
Dockerfile                 构建镜像并内置 Camoufox 浏览器资产
docker-compose.yml         推荐部署配置
entrypoint.sh              容器入口；校验内置 Camoufox 资产并启动服务
start.sh / stop.sh         本地开发启停脚本
requirements.txt           Python 依赖
.env.example               环境变量示例
.github/workflows/         GHCR 镜像构建 workflow
tests/                     部署配置和资源策略回归测试
```

## 常见问题

### 为什么镜像会比普通 Python 服务大？

镜像内置了 Camoufox 浏览器资产，所以镜像体积会包含浏览器本体。这样做的好处是生产启动稳定，容器运行时不需要再访问外网下载浏览器。

### 为什么解题期间内存会升高？

Turnstile 求解需要真实浏览器。即使默认只启动一个浏览器进程，打开目标页面和 Cloudflare challenge 时仍会有明显峰值。当前默认策略解决的是“任务结束后不长期常驻高内存”，不是把浏览器运行峰值降到普通 HTTP 服务水平。

### Docker 面板显示的内存和 `/health` 不一致怎么办？

以 `/health` 的 `browser_process_rss_mb` 判断是否还有活跃浏览器进程。如果浏览器进程 RSS 已经为 0，而面板仍显示较高，通常是 Docker/cgroup 统计中包含 page cache 或统计口径差异。

### 什么时候需要调大 `TURNSTILE_SHM_SIZE`？

如果日志中出现浏览器崩溃、页面进程退出、`Target closed` 一类问题，可以尝试：

```env
TURNSTILE_SHM_SIZE=1gb
```

更高并发或复杂页面再考虑 `2gb`。

### 如何开启鉴权？

在 compose 中设置：

```yaml
environment:
  API_KEY: "change-me-to-a-secret"
```

之后 `/createTask` 和 `/getTaskResult` 请求必须传相同的 `clientKey`。

## License

MIT，见 [LICENSE](LICENSE)。
