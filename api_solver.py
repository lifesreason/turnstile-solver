import os
import sys
import time
import uuid
import random
import logging
import asyncio
import json
from typing import Optional, Union
import argparse
from quart import Quart, request, jsonify
from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox
from db_results import init_db, save_result, load_result, cleanup_old_results
from browser_configs import browser_config
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box



COLORS = {
    'MAGENTA': '\033[35m',
    'BLUE': '\033[34m',
    'GREEN': '\033[32m',
    'YELLOW': '\033[33m',
    'RED': '\033[31m',
    'RESET': '\033[0m',
}

WORKER_RESULT_PREFIX = "__TURNSTILE_WORKER_RESULT__="


class CustomLogger(logging.Logger):
    @staticmethod
    def format_message(level, color, message):
        timestamp = time.strftime('%H:%M:%S')
        return f"[{timestamp}] [{COLORS.get(color)}{level}{COLORS.get('RESET')}] -> {message}"

    def debug(self, message, *args, **kwargs):
        super().debug(self.format_message('DEBUG', 'MAGENTA', message), *args, **kwargs)

    def info(self, message, *args, **kwargs):
        super().info(self.format_message('INFO', 'BLUE', message), *args, **kwargs)

    def success(self, message, *args, **kwargs):
        super().info(self.format_message('SUCCESS', 'GREEN', message), *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        super().warning(self.format_message('WARNING', 'YELLOW', message), *args, **kwargs)

    def error(self, message, *args, **kwargs):
        super().error(self.format_message('ERROR', 'RED', message), *args, **kwargs)


logging.setLoggerClass(CustomLogger)
logger: CustomLogger = logging.getLogger("TurnstileAPIServer")  # type: ignore
logger.setLevel(logging.DEBUG)
handler = logging.StreamHandler(sys.stdout)
logger.addHandler(handler)


class TurnstileAPIServer:

    def __init__(self, headless: bool, useragent: Optional[str], debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool = False, browser_name: Optional[str] = None, browser_version: Optional[str] = None):
        self.app = Quart(__name__)
        self.debug = debug
        self.browser_type = browser_type
        self.headless = headless
        self.thread_count = max(1, int(thread or 1))
        self.browser_instance_count = self._resolve_browser_instance_count()
        self.proxy_support = proxy_support
        self.browser_pool = asyncio.Queue()
        self.use_random_config = use_random_config
        self.browser_name = browser_name
        self.browser_version = browser_version
        self.console = Console()

        # Lazy pool: do not keep Camoufox/Chromium warm while idle.
        # TURNSTILE_LAZY=1 (default) starts browsers on first solve request.
        # TURNSTILE_IDLE_SEC reclaims the pool after quiet period.
        # TURNSTILE_BROWSER_INSTANCES keeps process count below concurrency slots.
        keep_alive_raw = (os.getenv("TURNSTILE_KEEP_BROWSER_ALIVE", "1") or "1").strip().lower()
        self.keep_browser_alive = keep_alive_raw in ("1", "true", "yes", "on")
        low_resource_raw = (os.getenv("TURNSTILE_LOW_RESOURCE_MODE", "0") or "0").strip().lower()
        self.low_resource_mode = low_resource_raw not in ("0", "false", "no", "off")
        self.camoufox_profile = (
            os.getenv("TURNSTILE_CAMOUFOX_PROFILE", "compact") or "compact"
        ).strip().lower()
        unblock_raw = (os.getenv("TURNSTILE_UNBLOCK_RENDERING", "0") or "0").strip().lower()
        self.unblock_rendering = unblock_raw in ("1", "true", "yes", "on")
        lazy_raw = (os.getenv("TURNSTILE_LAZY", "1") or "1").strip().lower()
        self.lazy_browsers = lazy_raw not in ("0", "false", "no", "off")
        try:
            self.idle_sec = float(os.getenv("TURNSTILE_IDLE_SEC", "60") or 60)
        except (TypeError, ValueError):
            self.idle_sec = 60.0
        if self.idle_sec < 0:
            self.idle_sec = 0.0
        self._pool_ready = False
        self._pool_lock: Optional[asyncio.Lock] = None
        self._owned_browsers: list = []
        self._playwright = None
        self._camoufox = None
        self._last_used = 0.0
        self._idle_task: Optional[asyncio.Task] = None
        self._in_flight = 0
        self._tasks_since_recycle = 0
        self._browser_pid_map: dict[int, set[int]] = {}
        try:
            # Recycle more often than the old 100-task default so long-running
            # keep-alive pools cannot drift toward multi-GB Firefox trees.
            self.browser_recycle_tasks = int(os.getenv("TURNSTILE_BROWSER_RECYCLE_TASKS", "25") or 25)
        except (TypeError, ValueError):
            self.browser_recycle_tasks = 25
        if self.browser_recycle_tasks < 0:
            self.browser_recycle_tasks = 0
        try:
            # Soft RSS guard: after a task, if browser tree exceeds this, rebuild.
            # 0 disables. Default 800MB catches the 1GB+ climb before 1.5GB panels.
            self.browser_recycle_rss_mb = float(
                os.getenv("TURNSTILE_BROWSER_RECYCLE_RSS_MB", "800") or 800
            )
        except (TypeError, ValueError):
            self.browser_recycle_rss_mb = 800.0
        if self.browser_recycle_rss_mb < 0:
            self.browser_recycle_rss_mb = 0.0
        try:
            self.pool_acquire_timeout_sec = float(
                os.getenv("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC", "30") or 30
            )
        except (TypeError, ValueError):
            self.pool_acquire_timeout_sec = 30.0
        if self.pool_acquire_timeout_sec <= 0:
            self.pool_acquire_timeout_sec = 30.0
        self.worker_mode = (os.getenv("TURNSTILE_WORKER_MODE", "inline") or "inline").strip().lower()
        if self.keep_browser_alive and self.worker_mode == "process":
            logger.warning(
                "Switching worker_mode from process to inline because "
                "TURNSTILE_KEEP_BROWSER_ALIVE=1 requires a reusable in-process browser pool"
            )
            self.worker_mode = "inline"
        try:
            self.worker_timeout = float(os.getenv("TURNSTILE_WORKER_TIMEOUT", "120") or 120)
        except (TypeError, ValueError):
            self.worker_timeout = 120.0
        if self.worker_timeout <= 0:
            self.worker_timeout = 120.0
        try:
            self.solve_timeout_sec = float(os.getenv("TURNSTILE_SOLVE_TIMEOUT_SEC", "60") or 60)
        except (TypeError, ValueError):
            self.solve_timeout_sec = 60.0
        if self.solve_timeout_sec <= 0:
            self.solve_timeout_sec = 60.0
        self._worker_tasks_queued = 0
        self._worker_tasks_running = 0
        self._worker_tasks_completed = 0
        self._worker_semaphore = asyncio.Semaphore(self.thread_count)

        # Initialize useragent and sec_ch_ua attributes
        self.useragent = useragent
        self.sec_ch_ua = None


        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            if browser_name and browser_version:
                config = browser_config.get_browser_config(browser_name, browser_version)
                if config:
                    useragent, sec_ch_ua = config
                    self.useragent = useragent
                    self.sec_ch_ua = sec_ch_ua
            elif useragent:
                self.useragent = useragent
            else:
                browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                self.browser_name = browser
                self.browser_version = version
                self.useragent = useragent
                self.sec_ch_ua = sec_ch_ua

        self.browser_args = []
        if self.useragent:
            self.browser_args.append(f"--user-agent={self.useragent}")

        self._setup_routes()

    def _resolve_browser_instance_count(self) -> int:
        raw_value = os.getenv("TURNSTILE_BROWSER_INSTANCES", "1")
        try:
            requested = int(str(raw_value or "1").strip())
        except (TypeError, ValueError):
            requested = 1
        return max(1, min(self.thread_count, requested))

    def _read_process_table(self) -> dict[int, dict]:
        """Read a small Linux /proc process table for cleanup and diagnostics."""
        proc_root = "/proc"
        processes: dict[int, dict] = {}
        if not os.path.isdir(proc_root):
            return processes

        for name in os.listdir(proc_root):
            if not name.isdigit():
                continue
            pid = int(name)
            stat_path = os.path.join(proc_root, name, "stat")
            status_path = os.path.join(proc_root, name, "status")
            try:
                stat = open(stat_path, encoding="utf-8").read()
                prefix, rest = stat.rsplit(") ", 1)
                command = prefix.split("(", 1)[1]
                stat_parts = rest.split()
                ppid = int(stat_parts[1])
                cpu_ticks = 0
                if len(stat_parts) > 12:
                    cpu_ticks = int(stat_parts[11]) + int(stat_parts[12])
            except Exception:
                continue

            rss_kb = 0
            try:
                with open(status_path, encoding="utf-8") as status_file:
                    for line in status_file:
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            break
            except Exception:
                pass

            processes[pid] = {
                "pid": pid,
                "ppid": ppid,
                "command": command,
                "rss_kb": rss_kb,
                "cpu_ticks": cpu_ticks,
            }

        return processes

    def _descendant_pids(self, root_pid: int | None = None, process_table: Optional[dict[int, dict]] = None) -> set[int]:
        root_pid = int(root_pid or os.getpid())
        processes = process_table if process_table is not None else self._read_process_table()
        children_by_parent: dict[int, list[int]] = {}
        for pid, info in processes.items():
            children_by_parent.setdefault(int(info.get("ppid") or 0), []).append(pid)

        descendants: set[int] = set()
        stack = list(children_by_parent.get(root_pid, []))
        while stack:
            pid = stack.pop()
            if pid in descendants:
                continue
            descendants.add(pid)
            stack.extend(children_by_parent.get(pid, []))
        return descendants

    def _snapshot_child_pids(self) -> set[int]:
        return self._descendant_pids(os.getpid())

    def _browser_like_pids(self, process_table: Optional[dict[int, dict]] = None) -> set[int]:
        processes = process_table if process_table is not None else self._read_process_table()
        descendants = self._descendant_pids(os.getpid(), processes)
        browser_names = ("camoufox", "firefox", "chromium", "chrome", "playwright")
        return {
            pid
            for pid in descendants
            if any(name in str(processes.get(pid, {}).get("command", "")).lower() for name in browser_names)
        }

    def _remember_browser_processes(self, browser, before_pids: set[int]) -> None:
        after_pids = self._snapshot_child_pids()
        browser_pids = set(after_pids - before_pids)
        for attr in ("process", "_process"):
            proc = getattr(browser, attr, None)
            pid = getattr(proc, "pid", None)
            if pid:
                browser_pids.add(int(pid))
        self._browser_pid_map[id(browser)] = browser_pids

    def display_welcome(self):
        """Displays welcome screen with logo."""
        self.console.clear()
        
        combined_text = Text()
        combined_text.append("\n📢 Channel: ", style="bold white")
        combined_text.append("https://t.me/D3_vin", style="cyan")
        combined_text.append("\n💬 Chat: ", style="bold white")
        combined_text.append("https://t.me/D3vin_chat", style="cyan")
        combined_text.append("\n📁 GitHub: ", style="bold white")
        combined_text.append("https://github.com/D3-vin", style="cyan")
        combined_text.append("\n📁 Version: ", style="bold white")
        combined_text.append("1.2a", style="green")
        combined_text.append("\n")

        info_panel = Panel(
            Align.left(combined_text),
            title="[bold blue]Turnstile Solver[/bold blue]",
            subtitle="[bold magenta]Dev by D3vin[/bold magenta]",
            box=box.ROUNDED,
            border_style="bright_blue",
            padding=(0, 1),
            width=50
        )

        self.console.print(info_panel)
        self.console.print()




    def _setup_routes(self) -> None:
        """Set up the application routes."""
        self.app.before_serving(self._startup)
        self.app.route('/turnstile', methods=['GET'])(self.process_turnstile)
        self.app.route('/result', methods=['GET'])(self.get_result)
        # YesCaptcha / CapSolver 兼容协议
        self.app.route('/createTask', methods=['POST'])(self.create_task)
        self.app.route('/getTaskResult', methods=['POST'])(self.get_task_result)
        # Memory/ops helpers
        self.app.route('/health', methods=['GET'])(self.health)
        self.app.route('/reclaim', methods=['POST', 'GET'])(self.reclaim)
        self.app.route('/')(self.index)
        

    async def _startup(self) -> None:
        """Boot HTTP + DB; optionally warm browsers (or wait for first task)."""
        self.display_welcome()
        self._pool_lock = asyncio.Lock()
        try:
            await init_db()
            # Periodic result cleanup (independent of browsers)
            asyncio.create_task(self._periodic_cleanup())

            if self.lazy_browsers:
                logger.info(
                    f"Lazy browser mode ON — pool starts on first captcha "
                    f"(concurrency_slots={self.thread_count}, "
                    f"browser_instances={self.browser_instance_count}, "
                    f"keep_alive={self.keep_browser_alive}, "
                    f"low_resource={self.low_resource_mode}, "
                    f"camoufox_profile={self.camoufox_profile}, "
                    f"worker_mode={self.worker_mode}, "
                    f"recycle_tasks={self.browser_recycle_tasks}, "
                    f"recycle_rss_mb={self.browser_recycle_rss_mb:.0f}, "
                    f"solve_timeout={self.solve_timeout_sec:.0f}s, "
                    f"idle_reclaim={self.idle_sec:.0f}s)"
                )
                if self.keep_browser_alive and self.idle_sec > 0:
                    self._idle_task = asyncio.create_task(self._idle_reaper())
            else:
                logger.info("Starting browser initialization (eager)")
                await self._initialize_browser()
                self._pool_ready = True
                self._last_used = time.time()
                if self.keep_browser_alive and self.idle_sec > 0:
                    self._idle_task = asyncio.create_task(self._idle_reaper())
        except Exception as e:
            logger.error(f"Failed to start turnstile solver: {str(e)}")
            raise

    async def _initialize_browser(self) -> None:
        """Initialize the browser and create the page pool."""
        # Drain any leftover entries before rebuilding.
        await self._drain_pool_discard()

        playwright = None
        camoufox = None

        if self.browser_type in ['chromium', 'chrome', 'msedge']:
            from patchright.async_api import async_playwright
            playwright = await async_playwright().start()
            self._playwright = playwright
        elif self.browser_type == "camoufox":
            camoufox_options = self._camoufox_launch_options()
            try:
                camoufox = AsyncCamoufox(**camoufox_options)
            except TypeError as e:
                if "firefox_user_prefs" not in str(e):
                    raise
                logger.warning(
                    "Camoufox rejected firefox_user_prefs; retrying without compact prefs"
                )
                camoufox_options = dict(camoufox_options)
                camoufox_options.pop("firefox_user_prefs", None)
                camoufox = AsyncCamoufox(**camoufox_options)
            self._camoufox = camoufox

        browser_configs = []
        for _ in range(self.thread_count):
            if self.browser_type in ['chromium', 'chrome', 'msedge']:
                if self.use_random_config:
                    browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                elif self.browser_name and self.browser_version:
                    config = browser_config.get_browser_config(self.browser_name, self.browser_version)
                    if config:
                        useragent, sec_ch_ua = config
                        browser = self.browser_name
                        version = self.browser_version
                    else:
                        browser, version, useragent, sec_ch_ua = browser_config.get_random_browser_config(self.browser_type)
                else:
                    browser = getattr(self, 'browser_name', 'custom')
                    version = getattr(self, 'browser_version', 'custom')
                    useragent = self.useragent
                    sec_ch_ua = getattr(self, 'sec_ch_ua', '')
            else:
                # Для camoufox и других браузеров используем значения по умолчанию
                browser = self.browser_type
                version = 'custom'
                useragent = self.useragent
                sec_ch_ua = getattr(self, 'sec_ch_ua', '')


            browser_configs.append({
                'browser_name': browser,
                'browser_version': version,
                'useragent': useragent,
                'sec_ch_ua': sec_ch_ua
            })

        owned = []
        browsers = []
        for i in range(self.browser_instance_count):
            config = browser_configs[i]

            browser_args = [
                "--window-position=0,0",
                "--force-device-scale-factor=1"
            ]
            if config['useragent']:
                browser_args.append(f"--user-agent={config['useragent']}")

            before_pids = self._snapshot_child_pids()
            browser = None
            if self.browser_type in ['chromium', 'chrome', 'msedge'] and playwright:
                browser = await playwright.chromium.launch(
                    channel=self.browser_type,
                    headless=self.headless,
                    args=browser_args
                )
            elif self.browser_type == "camoufox" and camoufox:
                browser = await camoufox.start()

            if browser:
                self._remember_browser_processes(browser, before_pids)
                item = (i + 1, browser, config)
                owned.append(item)
                browsers.append(browser)

            if self.debug:
                logger.info(f"Browser instance {i + 1} initialized successfully with {config['browser_name']} {config['browser_version']}")

        if not browsers:
            raise RuntimeError("No browser instances were initialized")

        for slot_index in range(self.thread_count):
            browser = browsers[slot_index % len(browsers)]
            config = browser_configs[slot_index]
            await self.browser_pool.put((slot_index + 1, browser, config))

        self._owned_browsers = owned
        self._pool_ready = True
        self._last_used = time.time()
        logger.info(
            f"Browser pool initialized with {self.browser_pool.qsize()} "
            f"concurrency slots over {len(self._owned_browsers)} browser instances"
        )

        if self.use_random_config:
            logger.info(f"Each browser in pool received random configuration")
        elif self.browser_name and self.browser_version:
            logger.info(f"All browsers using configuration: {self.browser_name} {self.browser_version}")
        else:
            logger.info("Using custom configuration")

        if self.debug:
            for i, config in enumerate(browser_configs):
                logger.debug(f"Browser slot {i+1} config: {config['browser_name']} {config['browser_version']}")
                logger.debug(f"Browser slot {i+1} User-Agent: {config['useragent']}")
                logger.debug(f"Browser slot {i+1} Sec-CH-UA: {config['sec_ch_ua']}")
            if self.browser_type == "camoufox":
                logger.debug(f"Camoufox launch options: {self._camoufox_launch_options()}")

    def _camoufox_user_prefs(self) -> dict:
        profile = (self.camoufox_profile or "compact").strip().lower()
        if profile in ("0", "off", "none", "false", "no"):
            return {}

        # Shared baseline: kill caches/prefetch so each solve does not leave
        # multi-hundred-MB of retained page state in the Firefox tree.
        prefs = {
            "browser.cache.disk.enable": False,
            "browser.cache.memory.enable": False,
            "browser.cache.memory.capacity": 0,
            "browser.cache.offline.enable": False,
            "browser.sessionhistory.max_total_viewers": 0,
            "browser.sessionstore.max_tabs_undo": 0,
            "browser.sessionstore.max_windows_undo": 0,
            "browser.sessionstore.resume_from_crash": False,
            "browser.sessionstore.interval": 600000,
            "media.peerconnection.enabled": False,
            "media.autoplay.default": 5,
            "media.autoplay.enabled": False,
            "media.navigator.enabled": False,
            "network.dns.disablePrefetch": True,
            "network.predictor.enabled": False,
            "network.prefetch-next": False,
            "network.http.speculative-parallel-limit": 0,
            "toolkit.telemetry.enabled": False,
            "memory.free_dirty_pages": True,
        }

        if profile == "compact" or self.low_resource_mode:
            # Single content process, no warm keep-alive, no fission — the
            # dominant lever against 1GB+ Camoufox trees under keep-alive.
            prefs.update({
                "dom.ipc.keepProcessesAlive.web": 0,
                "dom.ipc.keepProcessesAlive.webIsolated.perOrigin": 0,
                "dom.ipc.processCount": 1,
                "dom.ipc.processCount.webIsolated": 1,
                "dom.ipc.processPrelaunch.enabled": False,
                "fission.autostart": False,
                # Software compositing is cheaper and more predictable in Docker
                # than WebRender/GPU paths for a 500x100 captcha viewport.
                "layers.acceleration.disabled": True,
                "gfx.webrender.force-disabled": True,
                # Route handler already aborts images; this is a second belt so
                # any missed request cannot decode large bitmaps into RSS.
                "permissions.default.image": 2,
                "image.animation_mode": "none",
            })
        elif profile == "balanced":
            prefs.update({
                "dom.ipc.keepProcessesAlive.web": 0,
                "dom.ipc.keepProcessesAlive.webIsolated.perOrigin": 0,
                "dom.ipc.processCount": 2,
                "dom.ipc.processCount.webIsolated": 1,
                "dom.ipc.processPrelaunch.enabled": False,
                "fission.autostart": False,
            })

        return prefs

    def _camoufox_launch_options(self) -> dict:
        profile_enabled = (self.camoufox_profile or "compact").strip().lower() not in (
            "0", "off", "none", "false", "no"
        )
        options = {
            "headless": self.headless,
            "enable_cache": False,
            "exclude_addons": [DefaultAddons.UBO],
        }
        # Compact/low-resource always cut WebRTC and COOP cost. block_images is
        # intentionally NOT set here (compatibility); route + prefs handle assets.
        if self.low_resource_mode or profile_enabled:
            options.update({
                "block_webrtc": True,
                "disable_coop": True,
            })
        prefs = self._camoufox_user_prefs()
        if prefs:
            options["firefox_user_prefs"] = prefs
        return options

    def _camoufox_launch_options_summary(self) -> dict:
        options = self._camoufox_launch_options()
        return {
            key: [str(item) for item in value] if isinstance(value, list) else value
            for key, value in options.items()
        }

    async def _drain_pool_discard(self) -> None:
        """Empty the asyncio queue without closing browsers (caller closes)."""
        while True:
            try:
                self.browser_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:
                break

    async def _discard_browser_slots(self, bad_browser) -> int:
        """Remove queued concurrency slots that point to a dead shared browser."""
        kept = []
        removed = 0
        while True:
            try:
                item = self.browser_pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            except Exception:
                break

            try:
                _index, queued_browser, _config = item
            except Exception:
                continue
            if queued_browser is bad_browser:
                removed += 1
            else:
                kept.append(item)

        for item in kept:
            await self.browser_pool.put(item)

        return removed

    async def _close_maybe_async(self, obj, *method_names: str, label: str = "resource") -> bool:
        """Best-effort close helper for browser/driver objects."""
        if obj is None:
            return False
        for meth_name in method_names:
            meth = getattr(obj, meth_name, None)
            if meth is None:
                continue
            try:
                if meth_name == "__aexit__":
                    result = meth(None, None, None)
                else:
                    result = meth()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=8.0)
                logger.debug(f"{label}: {meth_name} ok")
                return True
            except Exception as e:
                if self.debug:
                    logger.warning(f"{label}: {meth_name} failed: {e}")
        return False

    def _kill_pid_tree(self, pid: int | None) -> None:
        """Force-kill one process and its descendants without killing our group."""
        if not pid:
            return
        pid = int(pid)
        if pid == os.getpid():
            return
        try:
            import signal

            processes = self._read_process_table()
            targets = self._descendant_pids(pid, processes)
            targets.add(pid)
            for target in sorted(targets, reverse=True):
                if target == os.getpid():
                    continue
                try:
                    os.kill(target, signal.SIGTERM)
                except Exception:
                    pass
            time.sleep(0.2)
            for target in sorted(targets, reverse=True):
                if target == os.getpid():
                    continue
                if os.path.exists(f"/proc/{target}"):
                    try:
                        os.kill(target, signal.SIGKILL)
                    except Exception:
                        pass
        except Exception as e:
            if self.debug:
                logger.warning(f"kill pid tree pid={pid} failed: {e}")

    def _kill_process_tree(self, proc) -> None:
        """Force-kill a browser child process tree (Camoufox/Chromium leftovers)."""
        self._kill_pid_tree(getattr(proc, "pid", None))

    def _kill_browser_process_leftovers(self) -> int:
        """Kill tracked and browser-like descendants left after wrapper close()."""
        pids: set[int] = set()
        for tracked in self._browser_pid_map.values():
            pids.update(tracked)
        pids.update(self._browser_like_pids())

        killed = 0
        for pid in sorted(pids, reverse=True):
            if pid == os.getpid():
                continue
            if os.path.exists(f"/proc/{pid}"):
                self._kill_pid_tree(pid)
                killed += 1
        self._browser_pid_map.clear()
        return killed

    def _process_memory_report(self) -> dict:
        processes = self._read_process_table()
        own_pid = os.getpid()
        children = self._descendant_pids(own_pid, processes)
        browser_pids = self._browser_like_pids(processes)

        def rss_mb(pid_set: set[int]) -> float:
            return round(sum(processes.get(pid, {}).get("rss_kb", 0) for pid in pid_set) / 1024.0, 1)

        def cpu_ticks(pid_set: set[int]) -> int:
            return int(sum(processes.get(pid, {}).get("cpu_ticks", 0) for pid in pid_set))

        own_rss = round(processes.get(own_pid, {}).get("rss_kb", 0) / 1024.0, 1)
        return {
            "process_rss_mb": own_rss,
            "process_cpu_ticks": int(processes.get(own_pid, {}).get("cpu_ticks", 0)),
            "children_count": len(children),
            "children_rss_mb": rss_mb(children),
            "children_cpu_ticks": cpu_ticks(children),
            "browser_process_count": len(browser_pids),
            "browser_process_rss_mb": rss_mb(browser_pids),
            "browser_cpu_ticks": cpu_ticks(browser_pids),
            "browser_processes": [
                {
                    "pid": pid,
                    "command": processes.get(pid, {}).get("command", ""),
                    "rss_mb": round(processes.get(pid, {}).get("rss_kb", 0) / 1024.0, 1),
                    "cpu_ticks": int(processes.get(pid, {}).get("cpu_ticks", 0)),
                }
                for pid in sorted(browser_pids)
            ],
        }

    async def _force_kill_browser(self, browser, index: int | None = None) -> None:
        """Hard cleanup for browsers that ignore close()/aclose()."""
        label = f"Browser {index}" if index is not None else "Browser"
        # Playwright-style browser process
        for attr in ("process", "_process"):
            proc = getattr(browser, attr, None)
            if proc is not None:
                self._kill_process_tree(proc)
                if self.debug:
                    logger.debug(f"{label}: force-killed via {attr}")
                return
        for pid in self._browser_pid_map.get(id(browser), set()):
            self._kill_pid_tree(pid)
        # Nested browser objects (some wrappers)
        for attr in ("browser", "_browser", "impl_obj", "_impl_obj"):
            nested = getattr(browser, attr, None)
            if nested is None or nested is browser:
                continue
            for nested_attr in ("process", "_process"):
                proc = getattr(nested, nested_attr, None)
                if proc is not None:
                    self._kill_process_tree(proc)
                    if self.debug:
                        logger.debug(f"{label}: force-killed nested {attr}.{nested_attr}")
                    return

    async def _shutdown_browsers(self) -> None:
        """Close every browser and release Playwright/Camoufox drivers."""
        items = list(self._owned_browsers or [])
        self._owned_browsers = []
        await self._drain_pool_discard()

        for index, browser, _config in items:
            closed = await self._close_maybe_async(
                browser, "close", "aclose", label=f"Browser {index}"
            )
            if not closed:
                await self._force_kill_browser(browser, index=index)
            else:
                # Even after close(), Camoufox occasionally leaves zombie children.
                # Best-effort hard cleanup when process handle is still visible.
                try:
                    await self._force_kill_browser(browser, index=index)
                except Exception:
                    pass
            if self.debug:
                logger.debug(f"Browser {index}: closed")

        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=8.0)
            except Exception as e:
                if self.debug:
                    logger.warning(f"Playwright stop failed: {e}")
            self._playwright = None

        if self._camoufox is not None:
            # AsyncCamoufox may expose aclose / __aexit__; best-effort.
            await self._close_maybe_async(
                self._camoufox, "aclose", "close", "__aexit__", label="Camoufox"
            )
            self._camoufox = None

        killed_leftovers = self._kill_browser_process_leftovers()
        if killed_leftovers:
            logger.warning(f"Force-killed {killed_leftovers} leftover browser process trees")

        # Idle reclaim must not keep a stuck counter forever.
        # If a solve task crashed without finally, _in_flight could block all future reclaim.
        if self._in_flight != 0:
            logger.warning(
                f"Resetting leaked in-flight counter during reclaim: was {self._in_flight}"
            )
            self._in_flight = 0

        self._pool_ready = False
        self._tasks_since_recycle = 0
        # Keep last_used as historical activity; do not bump it here or reclaim loops thrash.
        logger.info("Browser pool reclaimed (idle / rebuild)")

    def _should_recycle_browser_pool(self) -> tuple[bool, str]:
        """Decide whether the keep-alive pool should be torn down after a task."""
        if not self.keep_browser_alive or not self._pool_ready:
            # keep_browser_alive=0 always drops the pool after the task.
            if not self.keep_browser_alive and self._pool_ready:
                return True, "keep_browser_alive=0"
            return False, ""
        if self.browser_recycle_tasks > 0 and self._tasks_since_recycle >= self.browser_recycle_tasks:
            return True, (
                f"task_limit tasks={self._tasks_since_recycle} "
                f"limit={self.browser_recycle_tasks}"
            )
        if self.browser_recycle_rss_mb > 0:
            try:
                report = self._process_memory_report()
                browser_rss = float(report.get("browser_process_rss_mb") or 0)
                children_rss = float(report.get("children_rss_mb") or 0)
                rss = max(browser_rss, children_rss)
                if rss >= self.browser_recycle_rss_mb:
                    return True, (
                        f"rss_limit browser_rss={browser_rss:.0f}MB "
                        f"children_rss={children_rss:.0f}MB "
                        f"limit={self.browser_recycle_rss_mb:.0f}MB"
                    )
            except Exception as e:
                if self.debug:
                    logger.debug(f"RSS recycle check failed: {e}")
        return False, ""

    async def _reclaim_after_task_if_needed(self) -> None:
        """Drop browser processes when keep-alive is off, task/RSS limits hit."""
        should_recycle, reason = self._should_recycle_browser_pool()
        if self.keep_browser_alive and not should_recycle:
            return
        if self._in_flight > 0 or not self._pool_ready:
            return
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            should_recycle, reason = self._should_recycle_browser_pool()
            if self.keep_browser_alive and not should_recycle:
                return
            if self._in_flight > 0 or not self._pool_ready:
                return
            if should_recycle:
                logger.info(f"Recycling browser pool ({reason})")
            else:
                logger.info("Low-memory mode: reclaiming browser pool after task")
            await self._shutdown_browsers()

    async def _run_worker_subprocess(
        self,
        task_id: str,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> dict:
        task_payload = {
            "task_id": task_id,
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata,
            "proxy": proxy,
        }
        cmd = [
            sys.executable,
            os.path.abspath(__file__),
            "--worker-task-json",
            json.dumps(task_payload, separators=(",", ":")),
            "--browser_type",
            self.browser_type,
            "--thread",
            "1",
        ]
        if not self.headless:
            cmd.append("--no-headless")
        if self.debug:
            cmd.append("--debug")
        if self.proxy_support:
            cmd.append("--proxy")
        if self.use_random_config:
            cmd.append("--random")
        if self.browser_name:
            cmd.extend(["--browser", self.browser_name])
        if self.browser_version:
            cmd.extend(["--version", self.browser_version])

        env = os.environ.copy()
        env["TURNSTILE_WORKER_MODE"] = "inline"
        env["TURNSTILE_KEEP_BROWSER_ALIVE"] = "0"
        env["TURNSTILE_LAZY"] = "1"
        env["PYTHONUNBUFFERED"] = "1"

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        output = b""
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=self.worker_timeout)
            output = stdout or b""
        except asyncio.TimeoutError:
            logger.error(f"Worker timed out after {self.worker_timeout:.0f}s for task {task_id}")
            self._kill_pid_tree(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass
            return {
                "value": "CAPTCHA_FAIL",
                "elapsed_time": 0,
                "error": "worker_timeout",
                "resource_report": self._process_memory_report(),
            }

        text = output.decode("utf-8", errors="replace")
        for line in reversed(text.splitlines()):
            if line.startswith(WORKER_RESULT_PREFIX):
                try:
                    result = json.loads(line[len(WORKER_RESULT_PREFIX):])
                    if isinstance(result, dict):
                        return result
                except Exception as e:
                    return {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": f"worker_result_parse_error: {e}"}

        tail = "\n".join(text.splitlines()[-20:])
        return {
            "value": "CAPTCHA_FAIL",
            "elapsed_time": 0,
            "error": f"worker_missing_result exit={proc.returncode}: {tail}",
        }

    async def _solve_turnstile_in_worker(
        self,
        task_id: str,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> None:
        self._worker_tasks_queued += 1
        entered_worker = False
        try:
            async with self._worker_semaphore:
                self._worker_tasks_queued = max(0, self._worker_tasks_queued - 1)
                self._worker_tasks_running += 1
                entered_worker = True
                result = await self._run_worker_subprocess(
                    task_id=task_id,
                    url=url,
                    sitekey=sitekey,
                    action=action,
                    cdata=cdata,
                    proxy=proxy,
                )
                if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
                    logger.error(
                        f"Worker failed for task {task_id}: "
                        f"{result.get('error') or 'unknown_error'}"
                    )
                await save_result(task_id, "turnstile", result)
        finally:
            if not entered_worker:
                self._worker_tasks_queued = max(0, self._worker_tasks_queued - 1)
            else:
                self._worker_tasks_running = max(0, self._worker_tasks_running - 1)
                self._worker_tasks_completed += 1

    async def _ensure_pool(self) -> None:
        """Make sure the browser pool is warm before solving."""
        self._last_used = time.time()
        if self._pool_ready and self.browser_pool.qsize() > 0:
            return
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            self._last_used = time.time()
            if self._pool_ready and self.browser_pool.qsize() > 0:
                return
            # Rebuild if never ready, or all instances were dropped/disconnected.
            if self._pool_ready and self.browser_pool.empty() and self._in_flight > 0:
                # All browsers currently checked out — nothing to warm.
                return
            logger.info(
                f"Warming browser pool (concurrency_slots={self.thread_count}, "
                f"browser_instances={self.browser_instance_count}, type={self.browser_type})"
            )
            if self._pool_ready or self._owned_browsers or self._playwright or self._camoufox:
                await self._shutdown_browsers()
            await self._initialize_browser()

    async def _idle_reaper(self) -> None:
        """Close browsers after TURNSTILE_IDLE_SEC with no captcha activity."""
        # Check more frequently than idle window so reclaim is timely.
        interval = 15.0 if self.idle_sec <= 60 else min(30.0, max(10.0, self.idle_sec / 6.0))
        stuck_since = 0.0
        while True:
            try:
                await asyncio.sleep(interval)
                if self.idle_sec <= 0:
                    continue
                # Nothing warm / owned → nothing to reclaim.
                if not self._pool_ready and not self._owned_browsers:
                    stuck_since = 0.0
                    continue

                idle_for = time.time() - (self._last_used or 0.0)
                if idle_for < self.idle_sec:
                    stuck_since = 0.0
                    continue

                # Guard against leaked in-flight counters: if we have been idle longer
                # than 2x idle window and still see in-flight > 0, force reclaim.
                if self._in_flight > 0:
                    if stuck_since <= 0:
                        stuck_since = time.time()
                    stuck_for = time.time() - stuck_since
                    if stuck_for < max(self.idle_sec * 2.0, 120.0):
                        if self.debug:
                            logger.debug(
                                f"Idle reaper waiting: in_flight={self._in_flight}, "
                                f"idle={idle_for:.0f}s, stuck={stuck_for:.0f}s"
                            )
                        continue
                    logger.warning(
                        f"Idle reaper force-reclaim: in_flight stuck at {self._in_flight} "
                        f"for {stuck_for:.0f}s (idle={idle_for:.0f}s)"
                    )
                else:
                    stuck_since = 0.0

                if self._pool_lock is None:
                    self._pool_lock = asyncio.Lock()
                async with self._pool_lock:
                    idle_for = time.time() - (self._last_used or 0.0)
                    if idle_for < self.idle_sec:
                        continue
                    if self._in_flight > 0 and idle_for < max(self.idle_sec * 2.0, 120.0):
                        continue
                    owned_n = len(self._owned_browsers or [])
                    qsize = self.browser_pool.qsize()
                    logger.info(
                        f"No captcha for {idle_for:.0f}s — reclaiming "
                        f"queue={qsize} owned={owned_n} in_flight={self._in_flight}"
                    )
                    await self._shutdown_browsers()
                    stuck_since = 0.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Idle reaper error: {e}")

    async def _periodic_cleanup(self):
        """Periodic cleanup of old results every hour"""
        while True:
            try:
                await asyncio.sleep(3600)
                deleted_count = await cleanup_old_results(days_old=7)
                if deleted_count > 0:
                    logger.info(f"Cleaned up {deleted_count} old results")
            except Exception as e:
                logger.error(f"Error during periodic cleanup: {e}")

    async def _antishadow_inject(self, page):
        await page.add_init_script("""
          (function() {
            const originalAttachShadow = Element.prototype.attachShadow;
            Element.prototype.attachShadow = function(init) {
              const shadow = originalAttachShadow.call(this, init);
              if (init.mode === 'closed') {
                window.__lastClosedShadowRoot = shadow;
              }
              return shadow;
            };
          })();
        """)



    async def _optimized_route_handler(self, route):
        """Abort heavy assets; only document/script/XHR (and CF origins) load."""
        url = route.request.url
        resource_type = route.request.resource_type

        # Never load media/images/fonts/styles — they dominate decode CPU + RSS.
        blocked_types = {
            "image",
            "media",
            "font",
            "stylesheet",
            "texttrack",
            "eventsource",
            "websocket",
            "manifest",
            "other",
        }
        if resource_type in blocked_types:
            await route.abort()
            return

        allowed_types = {"document", "script", "xhr", "fetch"}

        allowed_domains = [
            "challenges.cloudflare.com",
            "static.cloudflareinsights.com",
            "cloudflare.com",
        ]

        if resource_type in allowed_types:
            await route.continue_()
        elif any(domain in url for domain in allowed_domains):
            # CF challenge scripts/XHR only — heavy types already aborted above.
            await route.continue_()
        else:
            await route.abort()

    async def _block_rendering(self, page):
        """Блокировка рендеринга для экономии ресурсов"""
        await page.route("**/*", self._optimized_route_handler)

    async def _unblock_rendering(self, page):
        """Разблокировка рендеринга"""
        await page.unroute("**/*", self._optimized_route_handler)

    async def _find_turnstile_elements(self, page, index: int):
        """Умная проверка всех возможных Turnstile элементов"""
        selectors = [
            '.cf-turnstile',
            '[data-sitekey]',
            'iframe[src*="turnstile"]',
            'iframe[title*="widget"]',
            'div[id*="turnstile"]',
            'div[class*="turnstile"]'
        ]
        
        elements = []
        for selector in selectors:
            try:
                # Безопасная проверка count()
                try:
                    count = await page.locator(selector).count()
                except Exception:
                    # Если count() дает ошибку, пропускаем этот селектор
                    continue
                    
                if count > 0:
                    elements.append((selector, count))
                    if self.debug:
                        logger.debug(f"Browser {index}: Found {count} elements with selector '{selector}'")
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Selector '{selector}' failed: {str(e)}")
                continue
        
        return elements

    async def _find_and_click_checkbox(self, page, index: int):
        """Найти и кликнуть по чекбоксу Turnstile CAPTCHA внутри iframe"""
        try:
            # Пробуем разные селекторы iframe с защитой от ошибок
            iframe_selectors = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                'iframe[title*="widget"]'
            ]
            
            iframe_locator = None
            for selector in iframe_selectors:
                try:
                    test_locator = page.locator(selector).first
                    # Безопасная проверка count для iframe
                    try:
                        iframe_count = await test_locator.count()
                    except Exception:
                        iframe_count = 0
                        
                    if iframe_count > 0:
                        iframe_locator = test_locator
                        if self.debug:
                            logger.debug(f"Browser {index}: Found Turnstile iframe with selector: {selector}")
                        break
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Iframe selector '{selector}' failed: {str(e)}")
                    continue
            
            if iframe_locator:
                try:
                    # Получаем frame из iframe
                    iframe_element = await iframe_locator.element_handle()
                    frame = await iframe_element.content_frame()
                    
                    if frame:
                        # Ищем чекбокс внутри iframe
                        checkbox_selectors = [
                            'input[type="checkbox"]',
                            '.cb-lb input[type="checkbox"]',
                            'label input[type="checkbox"]'
                        ]
                        
                        for selector in checkbox_selectors:
                            try:
                                # Полностью избегаем locator.count() в iframe - используем альтернативный подход
                                try:
                                    # Пробуем кликнуть напрямую без count проверки
                                    checkbox = frame.locator(selector).first
                                    await checkbox.click(timeout=2000)
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Successfully clicked checkbox in iframe with selector '{selector}'")
                                    return True
                                except Exception as click_e:
                                    # Если прямой клик не сработал, записываем в debug но не падаем
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Direct checkbox click failed for '{selector}': {str(click_e)}")
                                    continue
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Iframe checkbox selector '{selector}' failed: {str(e)}")
                                continue
                    
                        # Если нашли iframe, но не смогли кликнуть чекбокс, пробуем клик по iframe
                        try:
                            if self.debug:
                                logger.debug(f"Browser {index}: Trying to click iframe directly as fallback")
                            await iframe_locator.click(timeout=1000)
                            return True
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Iframe direct click failed: {str(e)}")
                
                except Exception as e:
                    if self.debug:
                        logger.debug(f"Browser {index}: Failed to access iframe content: {str(e)}")
            
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: General iframe search failed: {str(e)}")
        
        return False

    async def _try_click_strategies(self, page, index: int):
        strategies = [
            ('checkbox_click', lambda: self._find_and_click_checkbox(page, index)),
            ('direct_widget', lambda: self._safe_click(page, '.cf-turnstile', index)),
            ('iframe_click', lambda: self._safe_click(page, 'iframe[src*="turnstile"]', index)),
            ('js_click', lambda: page.evaluate("document.querySelector('.cf-turnstile')?.click()")),
            ('sitekey_attr', lambda: self._safe_click(page, '[data-sitekey]', index)),
            ('any_turnstile', lambda: self._safe_click(page, '*[class*="turnstile"]', index)),
            ('xpath_click', lambda: self._safe_click(page, "//div[@class='cf-turnstile']", index))
        ]
        
        for strategy_name, strategy_func in strategies:
            try:
                result = await strategy_func()
                if result is True or result is None:  # None означает успех для большинства стратегий
                    if self.debug:
                        logger.debug(f"Browser {index}: Click strategy '{strategy_name}' succeeded")
                    return True
            except Exception as e:
                if self.debug:
                    logger.debug(f"Browser {index}: Click strategy '{strategy_name}' failed: {str(e)}")
                continue
        
        return False

    async def _safe_click(self, page, selector: str, index: int):
        """Полностью безопасный клик с максимальной защитой от ошибок"""
        try:
            # Пробуем кликнуть напрямую без count() проверки
            locator = page.locator(selector).first
            await locator.click(timeout=1000)
            return True
        except Exception as e:
            # Логируем ошибку только в debug режиме
            if self.debug and "Can't query n-th element" not in str(e):
                logger.debug(f"Browser {index}: Safe click failed for '{selector}': {str(e)}")
            return False

    async def _detect_turnstile_params(self, page, index: int) -> dict:
        """Read action/cData from an existing widget when the caller omitted them."""
        try:
            params = await page.evaluate("""
            () => {
              const el = document.querySelector('.cf-turnstile,[data-sitekey]');
              if (!el) return {};
              return {
                sitekey: el.getAttribute('data-sitekey') || '',
                action: el.getAttribute('data-action') || '',
                cdata: el.getAttribute('data-cdata') || el.getAttribute('data-cData') || ''
              };
            }
            """)
            if isinstance(params, dict):
                if self.debug and (params.get("action") or params.get("cdata")):
                    logger.debug(
                        f"Browser {index}: Detected Turnstile params "
                        f"action={params.get('action') or ''} cdata={'set' if params.get('cdata') else ''}"
                    )
                return params
        except Exception as e:
            if self.debug:
                logger.debug(f"Browser {index}: Detect Turnstile params failed: {e}")
        return {}

    async def _inject_captcha_directly(self, page, websiteKey: str, action: str = '', cdata: str = '', index: int = 0):
        """Inject CAPTCHA directly into the target website"""
        sitekey_js = json.dumps(websiteKey or "")
        action_js = json.dumps(action or "")
        cdata_js = json.dumps(cdata or "")
        action_attr = f"captchaDiv.setAttribute('data-action', {action_js});" if action else ""
        cdata_attr = f"captchaDiv.setAttribute('data-cdata', {cdata_js});" if cdata else ""
        action_option = f"action: {action_js}," if action else ""
        cdata_option = f"cData: {cdata_js}," if cdata else ""
        script = f"""
        window.__turnstileDiagnostics = {{
            scriptLoaded: false,
            rendered: false,
            lastError: null
        }};

        // Remove any existing turnstile widgets first
        document.querySelectorAll('.cf-turnstile').forEach(el => el.remove());
        document.querySelectorAll('[data-sitekey]').forEach(el => el.remove());
        
        // Create turnstile widget directly on the page
        const captchaDiv = document.createElement('div');
        captchaDiv.className = 'cf-turnstile';
        captchaDiv.setAttribute('data-sitekey', {sitekey_js});
        captchaDiv.setAttribute('data-callback', 'onTurnstileCallback');
        {action_attr}
        {cdata_attr}
        captchaDiv.style.position = 'fixed';
        captchaDiv.style.top = '20px';
        captchaDiv.style.left = '20px';
        captchaDiv.style.zIndex = '9999';
        captchaDiv.style.backgroundColor = 'white';
        captchaDiv.style.padding = '15px';
        captchaDiv.style.border = '2px solid #0f79af';
        captchaDiv.style.borderRadius = '8px';
        captchaDiv.style.boxShadow = '0 4px 12px rgba(0, 0, 0, 0.3)';
        
        // Add to body immediately
        document.body.appendChild(captchaDiv);
        
        // Load Turnstile script and render widget
        const loadTurnstile = () => {{
            const script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
            script.async = true;
            script.defer = true;
            script.onload = function() {{
                window.__turnstileDiagnostics.scriptLoaded = true;
                console.log('Turnstile script loaded');
                // Wait a bit for script to initialize
                setTimeout(() => {{
                    if (window.turnstile && window.turnstile.render) {{
                        try {{
                            window.turnstile.render(captchaDiv, {{
                                sitekey: {sitekey_js},
                                {action_option}
                                {cdata_option}
                                callback: function(token) {{
                                    window.__turnstileDiagnostics.solved = true;
                                    console.log('Turnstile solved with token:', token);
                                    // Create hidden input for token
                                    let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                                    if (!tokenInput) {{
                                        tokenInput = document.createElement('input');
                                        tokenInput.type = 'hidden';
                                        tokenInput.name = 'cf-turnstile-response';
                                        document.body.appendChild(tokenInput);
                                    }}
                                    tokenInput.value = token;
                                }},
                                'error-callback': function(error) {{
                                    window.__turnstileDiagnostics.lastError = String(error || 'unknown');
                                    console.log('Turnstile error:', error);
                                }}
                            }});
                            window.__turnstileDiagnostics.rendered = true;
                        }} catch (e) {{
                            window.__turnstileDiagnostics.lastError = String(e && (e.stack || e.message) || e);
                            console.log('Turnstile render error:', e);
                        }}
                    }} else {{
                        window.__turnstileDiagnostics.lastError = 'turnstile_api_not_available';
                        console.log('Turnstile API not available');
                    }}
                }}, 1000);
            }};
            script.onerror = function() {{
                window.__turnstileDiagnostics.lastError = 'failed_to_load_turnstile_script';
                console.log('Failed to load Turnstile script');
            }};
            document.head.appendChild(script);
        }};
        
        // Check if Turnstile is already loaded
        if (window.turnstile) {{
            console.log('Turnstile already loaded, rendering immediately');
            try {{
                window.turnstile.render(captchaDiv, {{
                    sitekey: {sitekey_js},
                    {action_option}
                    {cdata_option}
                    callback: function(token) {{
                        window.__turnstileDiagnostics.solved = true;
                        console.log('Turnstile solved with token:', token);
                        let tokenInput = document.querySelector('input[name="cf-turnstile-response"]');
                        if (!tokenInput) {{
                            tokenInput = document.createElement('input');
                            tokenInput.type = 'hidden';
                            tokenInput.name = 'cf-turnstile-response';
                            document.body.appendChild(tokenInput);
                        }}
                        tokenInput.value = token;
                    }},
                    'error-callback': function(error) {{
                        window.__turnstileDiagnostics.lastError = String(error || 'unknown');
                        console.log('Turnstile error:', error);
                    }}
                }});
                window.__turnstileDiagnostics.rendered = true;
            }} catch (e) {{
                window.__turnstileDiagnostics.lastError = String(e && (e.stack || e.message) || e);
                console.log('Immediate render error:', e);
                loadTurnstile();
            }}
        }} else {{
            loadTurnstile();
        }}
        
        // Setup global callback
        window.onTurnstileCallback = function(token) {{
            console.log('Global turnstile callback executed:', token);
        }};
        """

        await page.evaluate(script)
        if self.debug:
            logger.debug(f"Browser {index}: Injected CAPTCHA directly into website with sitekey: {websiteKey}")

    def _build_context_options(self, browser_config: dict, proxy: Optional[str] = None) -> dict:
        """Build browser context options with Camoufox-safe defaults."""
        context_options: dict = {}

        # Camoufox + newer Playwright rejects default viewport.isMobile scheme.
        # Always disable default viewport and set size after page creation.
        context_options["no_viewport"] = True

        useragent = (browser_config or {}).get("useragent")
        if useragent:
            context_options["user_agent"] = useragent

        sec_ch_ua = (browser_config or {}).get("sec_ch_ua")
        if sec_ch_ua and str(sec_ch_ua).strip():
            context_options["extra_http_headers"] = {"sec-ch-ua": str(sec_ch_ua).strip()}

        if proxy:
            if "@" in proxy:
                scheme_part, auth_part = proxy.split("://", 1)
                auth, address = auth_part.split("@", 1)
                username, password = auth.split(":", 1)
                ip, port = address.split(":", 1)
                context_options["proxy"] = {
                    "server": f"{scheme_part}://{ip}:{port}",
                    "username": username,
                    "password": password,
                }
            else:
                parts = proxy.split(":")
                if len(parts) == 5:
                    proxy_scheme, proxy_ip, proxy_port, proxy_user, proxy_pass = parts
                    context_options["proxy"] = {
                        "server": f"{proxy_scheme}://{proxy_ip}:{proxy_port}",
                        "username": proxy_user,
                        "password": proxy_pass,
                    }
                elif len(parts) == 3:
                    context_options["proxy"] = {"server": proxy}
                else:
                    raise ValueError(f"Invalid proxy format: {proxy}")

        return context_options

    @staticmethod
    def _normalize_task_proxy(proxy: Optional[str]) -> Optional[str]:
        """Normalize per-task proxy string for Playwright/Camoufox context.

        Accepts:
          - http://user:pass@host:port
          - http://host:port
          - host:port
          - host:port:user:pass
          - scheme:host:port:user:pass
        """
        raw = str(proxy or "").strip()
        if not raw:
            return None
        if "://" in raw:
            return raw
        parts = raw.split(":")
        if len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        if len(parts) == 4 and parts[1].isdigit():
            # host:port:user:pass
            return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        if len(parts) == 5:
            # scheme:host:port:user:pass (legacy proxies.txt)
            return f"{parts[0]}://{parts[3]}:{parts[4]}@{parts[1]}:{parts[2]}"
        return raw

    @staticmethod
    def _redact_proxy(proxy: Optional[str]) -> str:
        raw = str(proxy or "").strip()
        if not raw:
            return ""
        # hide credentials for logs
        try:
            if "@" in raw and "://" in raw:
                scheme, rest = raw.split("://", 1)
                auth, hostpart = rest.rsplit("@", 1)
                return f"{scheme}://***:***@{hostpart}"
        except Exception:
            pass
        return raw

    def _pick_proxy(self, task_proxy: Optional[str] = None) -> Optional[str]:
        """Per-task proxy first; else optional proxies.txt when --proxy enabled."""
        normalized = self._normalize_task_proxy(task_proxy)
        if normalized:
            return normalized
        if not self.proxy_support:
            return None
        proxy_file_path = os.path.join(os.getcwd(), "proxies.txt")
        try:
            with open(proxy_file_path) as proxy_file:
                proxies = [line.strip() for line in proxy_file if line.strip()]
            if not proxies:
                return None
            return self._normalize_task_proxy(random.choice(proxies))
        except FileNotFoundError:
            logger.warning(f"Proxy file not found: {proxy_file_path}")
            return None
        except Exception as e:
            logger.error(f"Error reading proxy file: {str(e)}")
            return None

    async def _solve_turnstile(
        self,
        task_id: str,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        """Solve the Turnstile challenge."""
        context = None
        page = None
        start_time = time.time()
        index = None
        browser = None
        browser_config = None
        acquired = False

        # Mark in-flight before warm-up so the idle reaper cannot reclaim mid-acquire.
        # Always pair with the outer finally decrement — never leave this sticky.
        self._in_flight += 1
        try:
            try:
                await self._ensure_pool()
                self._last_used = time.time()
                # Never block forever on a leaked slot — that freezes the solver
                # while /health still returns ok and memory stays elevated.
                index, browser, browser_config = await asyncio.wait_for(
                    self.browser_pool.get(),
                    timeout=self.pool_acquire_timeout_sec,
                )
                acquired = True
                self._last_used = time.time()
            except asyncio.TimeoutError:
                logger.error(
                    f"Timed out acquiring browser after "
                    f"{self.pool_acquire_timeout_sec:.0f}s (queue empty / slot leaked)"
                )
                await save_result(task_id, "turnstile", {
                    "value": "CAPTCHA_FAIL",
                    "elapsed_time": 0,
                    "error": f"pool_acquire_timeout_{self.pool_acquire_timeout_sec:.0f}s",
                    "resource_report": self._process_memory_report(),
                })
                # Force rebuild so the next task is not stuck behind a dead pool.
                try:
                    if self._pool_lock is None:
                        self._pool_lock = asyncio.Lock()
                    async with self._pool_lock:
                        await self._shutdown_browsers()
                except Exception as reclaim_err:
                    logger.warning(f"Pool reclaim after acquire timeout failed: {reclaim_err}")
                return
            except Exception as e:
                logger.error(f"Failed to acquire browser from pool: {e}")
                await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": str(e)})
                return

            try:
                if hasattr(browser, 'is_connected') and not browser.is_connected():
                    if self.debug:
                        logger.warning(f"Browser {index}: Browser disconnected, skipping")
                    removed = await self._discard_browser_slots(browser)
                    if self.debug and removed:
                        logger.warning(f"Browser {index}: Removed {removed} queued slots for disconnected browser")
                    acquired = False
                    await save_result(task_id, "turnstile", {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": "browser_disconnected"})
                    return
            except Exception as e:
                if self.debug:
                    logger.warning(f"Browser {index}: Cannot check browser state: {str(e)}")

            # Per-task proxy takes priority; falls back to proxies.txt when --proxy is on.
            proxy = self._pick_proxy(proxy)
            if self.debug:
                if proxy:
                    logger.debug(f"Browser {index}: Creating context with proxy {self._redact_proxy(proxy)}")
                else:
                    logger.debug(f"Browser {index}: Creating context without proxy")

            context_options = self._build_context_options(browser_config or {}, proxy)
            try:
                context = await browser.new_context(**context_options)
            except Exception as ctx_err:
                # Fallback for Camoufox protocol mismatches / stricter option sets.
                if self.debug:
                    logger.warning(f"Browser {index}: new_context failed ({ctx_err}); retry minimal options")
                try:
                    context = await browser.new_context(no_viewport=True, **({"proxy": context_options.get("proxy")} if context_options.get("proxy") else {}))
                except Exception:
                    context = await browser.new_context(no_viewport=True)

            page = await context.new_page()
            if self.debug:
                page.on(
                    "console",
                    lambda msg: logger.debug(f"Browser {index}: console[{msg.type}] {msg.text}")
                )

            try:
                await page.set_viewport_size({"width": 500, "height": 100})
            except Exception:
                pass

            await self._antishadow_inject(page)
            await self._block_rendering(page)
            await page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
        });

        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
        };
        """)

            try:
                if self.debug:
                    logger.debug(f"Browser {index}: Starting Turnstile solve for URL: {url} with Sitekey: {sitekey} | Action: {action} | Cdata: {cdata} | Proxy: {proxy}")
                    logger.debug(f"Browser {index}: Setting up optimized page loading with resource blocking")
                    logger.debug(f"Browser {index}: Loading real website directly: {url}")

                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if self.unblock_rendering:
                    await self._unblock_rendering(page)

                if self.debug:
                    logger.debug(f"Browser {index}: Injecting Turnstile widget directly into target site")

                detected_params = await self._detect_turnstile_params(page, index)
                if not action:
                    action = detected_params.get("action") or action
                if not cdata:
                    cdata = detected_params.get("cdata") or cdata

                await self._inject_captcha_directly(page, sitekey, action or '', cdata or '', index)
                # Short settle; longer sleeps just keep Firefox CPU-hot while idle.
                await asyncio.sleep(1.0)

                locator = page.locator('input[name="cf-turnstile-response"]')
                # Bound by solve_timeout_sec; attempt count is a secondary ceiling.
                max_attempts = max(12, int(self.solve_timeout_sec * 2))
                click_count = 0
                max_clicks = 6
                solve_wait_started = time.time()
                deadline = solve_wait_started + self.solve_timeout_sec

                for attempt in range(max_attempts):
                    if time.time() >= deadline:
                        if self.debug:
                            logger.warning(
                                f"Browser {index}: Solve timeout reached "
                                f"({self.solve_timeout_sec:.0f}s)"
                            )
                        break
                    try:
                        try:
                            count = await locator.count()
                        except Exception as e:
                            if self.debug:
                                logger.debug(f"Browser {index}: Locator count failed on attempt {attempt + 1}: {str(e)}")
                            count = 0

                        if count == 0:
                            if self.debug and attempt % 5 == 0:
                                logger.debug(f"Browser {index}: No token elements found on attempt {attempt + 1}")
                        elif count == 1:
                            try:
                                token = await locator.input_value(timeout=500)
                                if token:
                                    elapsed_time = round(time.time() - start_time, 3)
                                    logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                    await save_result(task_id, "turnstile", {
                                        "value": token,
                                        "elapsed_time": elapsed_time,
                                        "resource_report": self._process_memory_report(),
                                    })
                                    return
                            except Exception as e:
                                if self.debug:
                                    logger.debug(f"Browser {index}: Single token element check failed: {str(e)}")
                        else:
                            if self.debug:
                                logger.debug(f"Browser {index}: Found {count} token elements, checking all")
                            for i in range(count):
                                try:
                                    element_token = await locator.nth(i).input_value(timeout=500)
                                    if element_token:
                                        elapsed_time = round(time.time() - start_time, 3)
                                        logger.success(f"Browser {index}: Successfully solved captcha - {COLORS.get('MAGENTA')}{element_token[:10]}{COLORS.get('RESET')} in {COLORS.get('GREEN')}{elapsed_time}{COLORS.get('RESET')} Seconds")
                                        await save_result(task_id, "turnstile", {
                                            "value": element_token,
                                            "elapsed_time": elapsed_time,
                                            "resource_report": self._process_memory_report(),
                                        })
                                        return
                                except Exception as e:
                                    if self.debug:
                                        logger.debug(f"Browser {index}: Token element {i} check failed: {str(e)}")
                                    continue

                        if attempt > 2 and attempt % 3 == 0 and click_count < max_clicks:
                            click_success = await self._try_click_strategies(page, index)
                            click_count += 1
                            if click_success and self.debug:
                                logger.debug(f"Browser {index}: Click successful (click #{click_count}/{max_clicks})")
                            elif not click_success and self.debug:
                                logger.debug(f"Browser {index}: All click strategies failed on attempt {attempt + 1} (click #{click_count}/{max_clicks})")

                        # Flat 0.5s poll — previous ramp to 2s stretched failed
                        # solves and kept CPU elevated longer than necessary.
                        await asyncio.sleep(0.5)

                        if self.debug and attempt % 5 == 0:
                            logger.debug(f"Browser {index}: Attempt {attempt + 1}/{max_attempts} - Waiting for token (clicks: {click_count}/{max_clicks})")

                    except Exception as e:
                        if self.debug:
                            logger.debug(f"Browser {index}: Attempt {attempt + 1} error: {str(e)}")
                        continue

                elapsed_time = round(time.time() - start_time, 3)
                diagnostics = {}
                try:
                    diagnostics = await page.evaluate("() => window.__turnstileDiagnostics || {}")
                except Exception:
                    diagnostics = {}
                error_detail = "timeout"
                if isinstance(diagnostics, dict) and diagnostics.get("lastError"):
                    error_detail = f"timeout: {diagnostics.get('lastError')}"
                elif time.time() >= deadline:
                    error_detail = f"solve_timeout_after_{self.solve_timeout_sec:.0f}s"
                await save_result(task_id, "turnstile", {
                    "value": "CAPTCHA_FAIL",
                    "elapsed_time": elapsed_time,
                    "error": error_detail,
                    "diagnostics": diagnostics if isinstance(diagnostics, dict) else {},
                    "resource_report": self._process_memory_report(),
                })
                if self.debug:
                    logger.error(
                        f"Browser {index}: Error solving Turnstile in "
                        f"{COLORS.get('RED')}{elapsed_time}{COLORS.get('RESET')} Seconds: {error_detail}"
                    )
            except Exception as e:
                elapsed_time = round(time.time() - start_time, 3)
                await save_result(task_id, "turnstile", {
                    "value": "CAPTCHA_FAIL",
                    "elapsed_time": elapsed_time,
                    "error": str(e),
                    "resource_report": self._process_memory_report(),
                })
                logger.error(f"Browser {index}: Error solving Turnstile: {str(e)}")
            finally:
                if self.debug:
                    logger.debug(f"Browser {index}: Closing browser context and cleaning up")

                if context is not None:
                    try:
                        await asyncio.wait_for(context.close(), timeout=5.0)
                        if self.debug:
                            logger.debug(f"Browser {index}: Context closed successfully")
                    except Exception as e:
                        # Stuck close() can pin a content process forever — drop
                        # the browser from the pool so the next task rebuilds.
                        acquired = False
                        logger.warning(
                            f"Browser {index}: Error closing context (discarding browser): {str(e)}"
                        )
                        try:
                            await self._discard_browser_slots(browser)
                            await self._force_kill_browser(browser, index=index)
                        except Exception:
                            pass

                try:
                    if acquired and browser is not None and index is not None:
                        connected = True
                        try:
                            if hasattr(browser, 'is_connected'):
                                connected = bool(browser.is_connected())
                        except Exception:
                            connected = True
                        if connected:
                            await self.browser_pool.put((index, browser, browser_config))
                            if self.debug:
                                logger.debug(f"Browser {index}: Browser returned to pool")
                        else:
                            removed = await self._discard_browser_slots(browser)
                            if self.debug:
                                logger.warning(f"Browser {index}: Browser disconnected, not returning to pool; removed {removed} queued slots")
                except Exception as e:
                    if self.debug:
                        logger.warning(f"Browser {index}: Error returning browser to pool: {str(e)}")
        finally:
            # Always release in-flight even on early return / unexpected exception.
            if browser is not None:
                self._tasks_since_recycle += 1
            if self._in_flight > 0:
                self._in_flight -= 1
            self._last_used = time.time()
            await self._reclaim_after_task_if_needed()






    def _check_client_key(self, client_key: Optional[str]) -> Optional[dict]:
        """校验 clientKey。未设置 API_KEY 时跳过鉴权。"""
        expected = os.getenv("API_KEY", "").strip()
        if not expected:
            return None
        if not client_key or client_key.strip() != expected:
            return {
                "errorId": 1,
                "errorCode": "ERROR_KEY_DOES_NOT_EXIST",
                "errorDescription": "Invalid clientKey"
            }
        return None

    async def _enqueue_turnstile(
        self,
        url: str,
        sitekey: str,
        action: Optional[str] = None,
        cdata: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        """创建任务并异步求解，返回 (task_id, error_response)。"""
        if not url or not sitekey:
            return None, {
                "errorId": 1,
                "errorCode": "ERROR_WRONG_PAGEURL",
                "errorDescription": "Both 'url' and 'sitekey' are required"
            }

        task_id = str(uuid.uuid4())
        await save_result(task_id, "turnstile", {
            "status": "CAPTCHA_NOT_READY",
            "createTime": int(time.time()),
            "url": url,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata,
            "proxy": self._redact_proxy(proxy) if proxy else "",
        })

        try:
            if self.worker_mode == "process":
                asyncio.create_task(
                    self._solve_turnstile_in_worker(
                        task_id=task_id,
                        url=url,
                        sitekey=sitekey,
                        action=action,
                        cdata=cdata,
                        proxy=proxy,
                    )
                )
            else:
                asyncio.create_task(
                    self._solve_turnstile(
                        task_id=task_id,
                        url=url,
                        sitekey=sitekey,
                        action=action,
                        cdata=cdata,
                        proxy=proxy,
                    )
                )
            if self.debug:
                logger.debug(
                    f"Request completed with taskid {task_id}"
                    + (f" proxy={self._redact_proxy(proxy)}" if proxy else " proxy=none")
                )
            return task_id, None
        except Exception as e:
            logger.error(f"Unexpected error processing request: {str(e)}")
            return None, {
                "errorId": 1,
                "errorCode": "ERROR_UNKNOWN",
                "errorDescription": str(e)
            }

    def _format_task_result(self, task_id: str, result) -> dict:
        """统一格式化任务结果（兼容 YesCaptcha）。"""
        if not result:
            return {
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": "Task not found"
            }

        if result == "CAPTCHA_NOT_READY" or (
            isinstance(result, dict) and result.get("status") == "CAPTCHA_NOT_READY"
        ):
            return {
                "errorId": 0,
                "status": "processing",
                "taskId": task_id,
            }

        if isinstance(result, dict) and result.get("value") == "CAPTCHA_FAIL":
            error_detail = str(result.get("error") or "Workers could not solve the Captcha")
            response = {
                "errorId": 1,
                "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
                "errorDescription": error_detail,
            }
            if result.get("elapsed_time") is not None:
                response["elapsedTime"] = result["elapsed_time"]
            if isinstance(result.get("diagnostics"), dict) and result.get("diagnostics"):
                response["diagnostics"] = result["diagnostics"]
            if isinstance(result.get("resource_report"), dict):
                response["resourceReport"] = result["resource_report"]
            return response

        if isinstance(result, dict) and result.get("value") and result.get("value") != "CAPTCHA_FAIL":
            response = {
                "errorId": 0,
                "status": "ready",
                "taskId": task_id,
                "solution": {
                    "token": result["value"]
                }
            }
            if result.get("elapsed_time") is not None:
                response["elapsedTime"] = result["elapsed_time"]
            if isinstance(result.get("resource_report"), dict):
                response["resourceReport"] = result["resource_report"]
            return response

        return {
            "errorId": 1,
            "errorCode": "ERROR_CAPTCHA_UNSOLVABLE",
            "errorDescription": "Workers could not solve the Captcha"
        }

    async def process_turnstile(self):
        """Handle the /turnstile endpoint requests."""
        url = request.args.get('url')
        sitekey = request.args.get('sitekey')
        action = request.args.get('action')
        cdata = request.args.get('cdata')
        proxy = request.args.get('proxy')

        task_id, err = await self._enqueue_turnstile(url, sitekey, action, cdata, proxy=proxy)
        if err:
            return jsonify(err), 200
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def get_result(self):
        """Return solved data"""
        task_id = request.args.get('id')

        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        return jsonify(self._format_task_result(task_id, result)), 200

    async def create_task(self):
        """YesCaptcha 兼容：POST /createTask"""
        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        auth_err = self._check_client_key(body.get("clientKey"))
        if auth_err:
            return jsonify(auth_err), 200

        task = body.get("task") or {}
        task_type = (task.get("type") or "").strip()
        # Local Camoufox solver only has one Turnstile path. Accept YesCaptcha /
        # CapSolver premium aliases (M1/M2) as Proxyless so registration clients
        # that default premium=True do not fail createTask with
        # ERROR_TASK_NOT_SUPPORTED before falling back.
        supported = {
            "TurnstileTaskProxyless",
            "TurnstileTaskProxylessM1",
            "TurnstileTaskProxylessM2",
            "TurnstileTask",
            "AntiTurnstileTaskProxyLess",
            "AntiTurnstileTaskProxyless",
            "AntiTurnstileTask",
        }
        if task_type and task_type not in supported:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_TASK_NOT_SUPPORTED",
                "errorDescription": f"Unsupported task type: {task_type}"
            }), 200

        url = task.get("websiteURL") or task.get("websiteUrl") or task.get("url")
        sitekey = task.get("websiteKey") or task.get("sitekey") or task.get("siteKey")
        action = task.get("action") or task.get("pageAction")
        cdata = task.get("cdata") or task.get("data")

        # CapSolver 风格 metadata
        metadata = task.get("metadata") or {}
        if isinstance(metadata, dict):
            action = action or metadata.get("action")
            cdata = cdata or metadata.get("cdata")

        # Per-task proxy (optional). Preferred over process-level proxies.txt.
        # Accept common shapes used by CapSolver / custom clients.
        proxy = (
            task.get("proxy")
            or task.get("proxyUrl")
            or task.get("proxyURL")
            or ""
        )
        if not proxy:
            # CapSolver-like split fields
            addr = str(task.get("proxyAddress") or task.get("proxyHost") or "").strip()
            port = str(task.get("proxyPort") or "").strip()
            user = str(task.get("proxyLogin") or task.get("proxyUsername") or "").strip()
            password = str(task.get("proxyPassword") or "").strip()
            if addr and port:
                if user:
                    proxy = f"http://{user}:{password}@{addr}:{port}"
                else:
                    proxy = f"http://{addr}:{port}"
        if isinstance(metadata, dict) and not proxy:
            proxy = metadata.get("proxy") or metadata.get("proxyUrl") or ""

        task_id, err = await self._enqueue_turnstile(
            url, sitekey, action, cdata, proxy=str(proxy or "").strip() or None
        )
        if err:
            return jsonify(err), 200
        return jsonify({"errorId": 0, "taskId": task_id}), 200

    async def get_task_result(self):
        """YesCaptcha 兼容：POST /getTaskResult"""
        try:
            body = await request.get_json(force=True, silent=True) or {}
        except Exception:
            body = {}

        auth_err = self._check_client_key(body.get("clientKey"))
        if auth_err:
            return jsonify(auth_err), 200

        task_id = body.get("taskId") or body.get("id")
        if not task_id:
            return jsonify({
                "errorId": 1,
                "errorCode": "ERROR_WRONG_CAPTCHA_ID",
                "errorDescription": "Invalid task ID/Request parameter"
            }), 200

        result = await load_result(task_id)
        return jsonify(self._format_task_result(task_id, result)), 200

    

    async def health(self):
        """Lightweight pool status for ops / memory debugging."""
        idle_for = None
        if self._last_used:
            idle_for = round(time.time() - self._last_used, 1)
        return jsonify({
            "ok": True,
            "lazy": bool(self.lazy_browsers),
            "keep_browser_alive": bool(self.keep_browser_alive),
            "low_resource_mode": bool(self.low_resource_mode),
            "unblock_rendering": bool(self.unblock_rendering),
            "camoufox_launch_options": self._camoufox_launch_options_summary() if self.browser_type == "camoufox" else None,
            "worker_mode": self.worker_mode,
            "worker_timeout": self.worker_timeout,
            "solve_timeout_sec": self.solve_timeout_sec,
            "worker_capacity": self.thread_count,
            "worker_queued": int(self._worker_tasks_queued or 0),
            "worker_running": int(self._worker_tasks_running or 0),
            "worker_completed": int(self._worker_tasks_completed or 0),
            "browser_recycle_tasks": self.browser_recycle_tasks,
            "browser_recycle_rss_mb": self.browser_recycle_rss_mb,
            "pool_acquire_timeout_sec": self.pool_acquire_timeout_sec,
            "tasks_since_recycle": self._tasks_since_recycle,
            "idle_sec": self.idle_sec,
            "pool_ready": bool(self._pool_ready),
            "thread": self.thread_count,
            "concurrency_slots": self.thread_count,
            "browser_instances": self.browser_instance_count,
            "browser_type": self.browser_type,
            "queue": self.browser_pool.qsize(),
            "owned": len(self._owned_browsers or []),
            "in_flight": int(self._in_flight or 0),
            "idle_for_sec": idle_for,
            **self._process_memory_report(),
        }), 200

    async def reclaim(self):
        """Force reclaim browser pool (manual memory drop)."""
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()
        async with self._pool_lock:
            owned = len(self._owned_browsers or [])
            qsize = self.browser_pool.qsize()
            in_flight = int(self._in_flight or 0)
            await self._shutdown_browsers()
        return jsonify({
            "ok": True,
            "reclaimed": True,
            "owned_before": owned,
            "queue_before": qsize,
            "in_flight_before": in_flight,
            "pool_ready": bool(self._pool_ready),
            "owned": len(self._owned_browsers or []),
            "browser_instances": self.browser_instance_count,
            "queue": self.browser_pool.qsize(),
            "in_flight": int(self._in_flight or 0),
            "tasks_since_recycle": self._tasks_since_recycle,
        }), 200

    @staticmethod
    async def index():
        """Serve the API documentation page."""
        return """
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Turnstile Solver API</title>
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-gray-900 text-gray-200 min-h-screen flex items-center justify-center">
                <div class="bg-gray-800 p-8 rounded-lg shadow-md max-w-2xl w-full border border-red-500">
                    <h1 class="text-3xl font-bold mb-6 text-center text-red-500">Welcome to Turnstile Solver API</h1>

                    <p class="mb-4 text-gray-300">支持两套协议：原生 GET，以及 YesCaptcha/CapSolver 兼容 POST。</p>

                    <h2 class="text-xl font-semibold mb-3 text-red-400">1) 原生协议</h2>
                    <ul class="list-disc pl-6 mb-4 text-gray-300">
                        <li><code>GET /turnstile?url=...&amp;sitekey=...</code> → 返回 taskId</li>
                        <li><code>GET /result?id=TASK_ID</code> → 轮询结果</li>
                    </ul>
                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">Example:</p>
                        <code class="text-sm break-all text-red-300">/turnstile?url=https://example.com&sitekey=0x4AAAA...</code>
                    </div>

                    <h2 class="text-xl font-semibold mb-3 text-red-400">2) YesCaptcha 兼容协议</h2>
                    <ul class="list-disc pl-6 mb-4 text-gray-300">
                        <li><code>POST /createTask</code></li>
                        <li><code>POST /getTaskResult</code></li>
                    </ul>
                    <div class="bg-gray-700 p-4 rounded-lg mb-6 border border-red-500">
                        <p class="font-semibold mb-2 text-red-400">createTask body:</p>
                        <pre class="text-sm text-red-300 whitespace-pre-wrap">{
  "clientKey": "optional-if-API_KEY-set",
  "task": {
    "type": "TurnstileTaskProxyless",
    "websiteURL": "https://example.com",
    "websiteKey": "0x4AAAA...",
    "proxy": "http://user:pass@host:port"
  }
}</pre>
                        <p class="text-xs text-gray-400 mt-2">proxy 可选：任务级代理优先；未传时若启动带 --proxy 则从 proxies.txt 随机。</p>
                    </div>


                    <div class="bg-gray-700 p-4 rounded-lg mb-6">
                        <p class="text-gray-200 font-semibold mb-3">📢 Connect with Us</p>
                        <div class="space-y-2 text-sm">
                            <p class="text-gray-300">
                                📢 <strong>Channel:</strong> 
                                <a href="https://t.me/D3_vin" class="text-red-300 hover:underline">https://t.me/D3_vin</a> 
                                - Latest updates and releases
                            </p>
                            <p class="text-gray-300">
                                💬 <strong>Chat:</strong> 
                                <a href="https://t.me/D3vin_chat" class="text-red-300 hover:underline">https://t.me/D3vin_chat</a> 
                                - Community support and discussions
                            </p>
                            <p class="text-gray-300">
                                📁 <strong>GitHub:</strong> 
                                <a href="https://github.com/D3-vin" class="text-red-300 hover:underline">https://github.com/D3-vin</a> 
                                - Source code and development
                            </p>
                        </div>
                    </div>
                </div>
            </body>
            </html>
        """


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Turnstile API Server")

    parser.add_argument('--no-headless', action='store_true', help='Run the browser with GUI (disable headless mode). By default, headless mode is enabled.')
    parser.add_argument('--useragent', type=str, help='User-Agent string (if not specified, random configuration is used)')
    parser.add_argument('--debug', action='store_true', help='Enable or disable debug mode for additional logging and troubleshooting information (default: False)')
    parser.add_argument('--browser_type', type=str, default='camoufox', help='Specify the browser type for the solver. Supported options: chromium, chrome, msedge, camoufox (default: camoufox)')
    parser.add_argument('--thread', type=int, default=1, help='Set the number of browser threads to use for multi-threaded mode. Increasing this will speed up execution but requires more resources (default: 1)')
    parser.add_argument('--proxy', action='store_true', help='Enable proxy support for the solver (Default: False)')
    parser.add_argument('--random', action='store_true', help='Use random User-Agent and Sec-CH-UA configuration from pool')
    parser.add_argument('--browser', type=str, help='Specify browser name to use (e.g., chrome, firefox)')
    parser.add_argument('--version', type=str, help='Specify browser version to use (e.g., 139, 141)')
    parser.add_argument('--host', type=str, default='0.0.0.0', help='Specify the IP address where the API solver runs. (Default: 127.0.0.1)')
    parser.add_argument('--port', type=str, default='5072', help='Set the port for the API solver to listen on. (Default: 5072)')
    parser.add_argument('--worker-task-json', type=str, help='Run one solve task as a worker and print the result JSON.')
    return parser.parse_args()


def create_app(headless: bool, useragent: str, debug: bool, browser_type: str, thread: int, proxy_support: bool, use_random_config: bool, browser_name: str, browser_version: str) -> Quart:
    server = TurnstileAPIServer(headless=headless, useragent=useragent, debug=debug, browser_type=browser_type, thread=thread, proxy_support=proxy_support, use_random_config=use_random_config, browser_name=browser_name, browser_version=browser_version)
    return server.app


async def _run_worker_from_args(args) -> int:
    try:
        task = json.loads(args.worker_task_json or "{}")
    except Exception as e:
        print(WORKER_RESULT_PREFIX + json.dumps({
            "value": "CAPTCHA_FAIL",
            "elapsed_time": 0,
            "error": f"invalid_worker_task_json: {e}",
        }, separators=(",", ":")))
        return 2

    task_id = str(task.get("task_id") or uuid.uuid4())
    server = TurnstileAPIServer(
        headless=not args.no_headless,
        debug=args.debug,
        useragent=args.useragent,
        browser_type=args.browser_type,
        thread=1,
        proxy_support=args.proxy,
        use_random_config=args.random,
        browser_name=args.browser,
        browser_version=args.version,
    )

    try:
        await server._solve_turnstile(
            task_id=task_id,
            url=str(task.get("url") or ""),
            sitekey=str(task.get("sitekey") or ""),
            action=task.get("action"),
            cdata=task.get("cdata"),
            proxy=task.get("proxy"),
        )
        result = await load_result(task_id)
        if not isinstance(result, dict):
            result = {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": "worker_result_not_found"}
    except Exception as e:
        result = {"value": "CAPTCHA_FAIL", "elapsed_time": 0, "error": str(e)}
    finally:
        try:
            await server._shutdown_browsers()
        except Exception as e:
            if isinstance(result, dict):
                result["cleanup_warning"] = f"worker_cleanup_failed: {e}"

    print(WORKER_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == '__main__':
    args = parse_args()
    if args.worker_task_json:
        raise SystemExit(asyncio.run(_run_worker_from_args(args)))

    browser_types = [
        'chromium',
        'chrome',
        'msedge',
        'camoufox',
    ]
    if args.browser_type not in browser_types:
        logger.error(f"Unknown browser type: {COLORS.get('RED')}{args.browser_type}{COLORS.get('RESET')} Available browser types: {browser_types}")
    else:
        app = create_app(
            headless=not args.no_headless, 
            debug=args.debug, 
            useragent=args.useragent, 
            browser_type=args.browser_type, 
            thread=args.thread, 
            proxy_support=args.proxy,
            use_random_config=args.random,
            browser_name=args.browser,
            browser_version=args.version
        )
        app.run(host=args.host, port=int(args.port))
