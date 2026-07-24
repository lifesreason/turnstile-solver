# turnstile-solver

`turnstile-solver` 是一个可独立部署的 Cloudflare Turnstile 求解服务。本仓库把浏览器求解逻辑封装为 HTTP API，供其它业务服务通过网络调用，避免每个业务项目都内置一套浏览器和验证码处理流程。

当前仓库重点面向 Docker / NAS / 服务器部署，并针对连续调用场景做了低资源复用配置：

- 镜像内置 Camoufox 浏览器资产，容器启动不依赖运行时下载。
- 服务启动后不立即启动浏览器，第一次收到解题任务时才懒加载。
- 默认只启动 1 个真实浏览器进程。
- 默认在主服务进程内复用 Camoufox，减少连续调用时反复冷启动造成的 CPU 峰值。
- 默认 300 秒没有新任务后释放浏览器，并在复用 8 个任务后强制回收一次。
- 浏览器子进程 RSS 超过约 700MB 时，任务结束后也会强制回收，避免长跑涨到 1GB+。
- 默认使用 Camoufox `compact` 资源档位：限制 Firefox 内容进程、关闭 cache/prefetch（不禁用 Turnstile 所需渲染能力）。
- 默认阻断非 Cloudflare 的重页面资源；Cloudflare challenge 资源始终放行。
- `/health` 提供浏览器池、进程 RSS 和浏览器子进程诊断信息。
- 所有运行参数都可通过同目录 `.env` + `docker-compose.yml` 的 `${VAR:-默认}` 赋值。

> 请只在你有授权的业务流程中使用本服务。Turnstile token 与访问页面、站点 key、出口 IP、会话状态等因素有关，生成后也有时效限制。

## 工作方式

服务启动后只运行一个轻量 HTTP 进程。业务调用 `createTask` 或 `/turnstile` 后，主服务默认懒加载 1 个 Camoufox 浏览器实例，打开目标页面、注入/定位 Turnstile 组件并等待 token。后续任务复用同一个浏览器池，避免连续调用时反复冷启动。

默认 `TURNSTILE_WORKER_MODE=inline` 适合持续调用。如果你更看重每个任务结束后主进程回到最低内存，可以设置 `TURNSTILE_KEEP_BROWSER_ALIVE=0` 且 `TURNSTILE_WORKER_MODE=process`，恢复每任务独立 worker 的隔离模式。

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
  "keep_browser_alive": true,
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
| `TURNSTILE_KEEP_BROWSER_ALIVE` | `1` | `1` 表示复用浏览器池；`0` 表示任务完成后立即关闭浏览器 |
| `TURNSTILE_LOW_RESOURCE_MODE` | `0` | `1` 表示 Camoufox 启用实验性低资源参数；默认关闭以保持兼容性 |
| `TURNSTILE_CAMOUFOX_PROFILE` | `compact` | Camoufox 资源档位：`compact` / `balanced` / `off` |
| `TURNSTILE_UNBLOCK_RENDERING` | `0` | `0` 表示持续阻断图片/字体/样式等重资源；兼容性问题可设 `1` |
| `TURNSTILE_WORKER_MODE` | `inline` | `inline` 表示在主服务内复用浏览器；`process` 表示每个任务使用独立 worker 子进程 |
| `TURNSTILE_WORKER_TIMEOUT` | `120` | worker 单任务超时秒数，超时后主服务会终止 worker 进程树 |
| `TURNSTILE_SOLVE_TIMEOUT_SEC` | `60` | 浏览器内实际等待 Turnstile token 的最长秒数，用于缩短失败任务高资源占用时间 |
| `TURNSTILE_BROWSER_RECYCLE_TASKS` | `8` | 常驻浏览器复用多少个任务后强制回收；`0` 表示不按任务数回收 |
| `TURNSTILE_BROWSER_RECYCLE_RSS_MB` | `700` | 浏览器/子进程 RSS 超过该阈值(MB)时任务结束后强制回收；`0` 关闭 |
| `TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC` | `120` | 从浏览器池取槽位超时秒数；其它任务正在使用浏览器时不会关闭活动浏览器 |
| `TURNSTILE_MAX_PENDING_TASKS` | `2` | 待处理任务上限；超过后拒绝新任务，防止调用方无限堆积内存 |
| `TURNSTILE_HEALTH_STUCK_SEC` | `180` | 最老任务超过该秒数后 `/health` 返回 503，交给 Docker 健康检查重启 |
| `TURNSTILE_IDLE_SEC` | `300` | 保温模式下的空闲回收秒数；仅 `TURNSTILE_KEEP_BROWSER_ALIVE=1` 时有意义 |
| `TURNSTILE_PROXY` | `0` | `1` 表示启用 `proxies.txt` 代理池 |
| `TURNSTILE_SHM_SIZE` | `512mb` | Docker `/dev/shm` 大小；复杂页面或更高并发可设 `1gb` / `2gb` |
| `CAMOUFOX_MIN_CACHE_MB` | `500` | 容器启动时校验内置 Camoufox 资产的最小体积 |
| `API_KEY` | 空 | 设置后请求必须带相同 `clientKey` |

## 资源模式

### 连续调用默认模式

默认配置适合持续调用同一个求解服务：

```env
TURNSTILE_THREAD=1
TURNSTILE_BROWSER_INSTANCES=1
TURNSTILE_KEEP_BROWSER_ALIVE=1
TURNSTILE_LOW_RESOURCE_MODE=0
TURNSTILE_CAMOUFOX_PROFILE=compact
TURNSTILE_UNBLOCK_RENDERING=0
TURNSTILE_WORKER_MODE=inline
TURNSTILE_WORKER_TIMEOUT=120
TURNSTILE_SOLVE_TIMEOUT_SEC=60
TURNSTILE_BROWSER_RECYCLE_TASKS=8
TURNSTILE_BROWSER_RECYCLE_RSS_MB=700
TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120
TURNSTILE_MAX_PENDING_TASKS=2
TURNSTILE_HEALTH_STUCK_SEC=180
TURNSTILE_IDLE_SEC=300
TURNSTILE_SHM_SIZE=512mb
```

特点：

- 启动后仍然只跑轻量 HTTP 进程，首次任务才启动 Camoufox。
- 连续调用时复用同一个 Camoufox，降低反复启动浏览器造成的 CPU 峰值。
- 300 秒无新任务后自动释放浏览器。
- 常驻浏览器每处理 8 个任务后强制回收一次；浏览器树 RSS 超过约 700MB 时也会回收，避免长跑涨到 1GB+。
- `compact` 档限制 Firefox 内容进程并关闭 cache/prefetch；**不要**默认开启 `TURNSTILE_LOW_RESOURCE_MODE=1`（容易导致 Turnstile 失败）。
- 如果 Turnstile 长时间没有返回 token，`TURNSTILE_SOLVE_TIMEOUT_SEC` 会提前结束浏览器求解，避免失败任务持续占用高 CPU / 高内存。
- 如果 compact 档影响通过率，先改成 `TURNSTILE_CAMOUFOX_PROFILE=balanced`；仍有问题再设 `off`。

### 每任务隔离模式

如果你更重视任务结束后立刻回到最低内存，可以切回每任务独立 worker：

```env
TURNSTILE_KEEP_BROWSER_ALIVE=0
TURNSTILE_WORKER_MODE=process
```

特点：

- 每个任务启动独立 worker 和浏览器，结束后退出。
- 调用结束后的常驻内存最低。
- 连续调用时 CPU 更容易被浏览器冷启动打高。

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
| `children_count` | 当前服务子进程数量 |
| `browser_process_rss_mb` | 浏览器相关子进程 RSS 总和 |
| `browser_process_count` | 浏览器相关子进程数量 |
| `browser_cpu_ticks` | 浏览器相关子进程累计 CPU ticks，用于判断任务期间 CPU 是否主要消耗在浏览器 |
| `cgroup_memory_current_mb` | Docker cgroup 当前内存计费，包含容器内存及部分缓存 |
| `cgroup_memory_peak_mb` | Docker cgroup 自启动以来记录的内存峰值 |
| `cgroup_cpu_percent` | 两次 `/health` 采样之间的 cgroup CPU 使用率；100% 约等于一个满载 CPU 核心 |
| `browser_processes` | 浏览器相关子进程列表 |
| `pool_ready` | 浏览器池是否仍处于可用状态 |
| `owned` | 当前服务持有的真实浏览器实例数量 |
| `queue` | 当前可用并发槽位数量 |
| `in_flight` | 当前正在处理的任务数量 |
| `worker_queued` | process worker 模式下等待执行的任务数量 |
| `worker_running` | process worker 模式下正在执行的任务数量 |
| `worker_completed` | process worker 模式下已完成任务数量 |
| `pending_tasks` | 当前已接收但尚未结束的任务数量 |
| `oldest_task_age_sec` | 当前最老任务已运行秒数 |
| `health_degraded` | 是否检测到任务长时间没有结束；为 `true` 时 HTTP 状态为 503 |
| `solve_timeout_sec` | 浏览器内求解阶段的超时秒数 |
| `browser_recycle_tasks` | 常驻浏览器按任务数回收的阈值 |
| `browser_recycle_rss_mb` | 浏览器/子进程 RSS 回收阈值(MB) |
| `pool_acquire_timeout_sec` | 取浏览器池槽位超时秒数 |
| `tasks_since_recycle` | 当前浏览器池已复用的任务数量 |

判断方式：

- `browser_process_rss_mb > 0`：还有浏览器进程在运行，可能是任务未结束、保温模式开启，或浏览器残留未清理。
- `worker_mode = inline` 且 `in_flight <= 1` 但 Docker 面板仍到 1GB+：这是单个 Camoufox/Firefox 进程树的峰值，不是 process worker 叠加。
- `worker_queued > 0`：调用方已经并行提交了多个任务；process 模式下会排队，不会同时启动多个 worker，但失败任务会拉长整体高资源窗口。
- `tasks_since_recycle` 接近 `browser_recycle_tasks`：下一次任务结束后会回收并重建浏览器池。
- `browser_process_rss_mb` 接近或超过 `browser_recycle_rss_mb`：下一任务结束后会按 RSS 强制回收。
- `browser_process_rss_mb = 0` 但 Docker 面板仍显示高：更可能是容器 page cache 或面板统计口径，不是活跃浏览器进程 RSS。
- `pool_ready = true` 且 `owned > 0`：浏览器池仍保温；确认是否设置了 `TURNSTILE_KEEP_BROWSER_ALIVE=1`。

### 继续降低单任务峰值的路线

如果确认 `worker_running = 1` 且 `browser_process_count` 只有一组浏览器进程，但峰值仍在 1GB 以上，说明主要开销来自 Camoufox/Firefox 本身。此时有三条路线：

1. 保持 Camoufox：成功率相对稳，但只能通过更短超时、禁用保温、限制并发来降低总资源时间，单次峰值下降有限。
2. 做 Chromium/Patchright A/B 测试：内存和 CPU 可能低一些，但 Turnstile 通过率需要用你的目标站点实测。默认 Docker 镜像为了控制体积没有打包 Patchright/Chromium；如果要走这条路，建议单独做一个 chromium flavor 镜像，而不是把两套浏览器都塞进默认镜像。
3. 将解题浏览器外置：主服务只做队列和协议转换，浏览器运行在独立容器、远程浏览器服务或第三方验证码服务中，主服务内存可保持在 100MB 左右，但资源成本会转移到外部组件。

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
TURNSTILE_KEEP_BROWSER_ALIVE: "1"
TURNSTILE_CAMOUFOX_PROFILE: compact
TURNSTILE_UNBLOCK_RENDERING: "0"
TURNSTILE_WORKER_MODE: inline
TURNSTILE_WORKER_TIMEOUT: "120"
TURNSTILE_BROWSER_RECYCLE_TASKS: "8"
TURNSTILE_BROWSER_RECYCLE_RSS_MB: "700"
TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC: "120"
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

Turnstile 求解需要真实浏览器。当前默认使用 Camoufox compact 档、单浏览器实例、资源拦截和 300 秒 idle 回收来压低峰值和持续时间。连续调用时浏览器会常驻复用；如果 300 秒没有新调用，会自动释放。

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
