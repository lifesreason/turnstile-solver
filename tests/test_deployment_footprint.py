import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentFootprintTest(unittest.TestCase):
    def test_compose_defaults_are_low_memory(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        # All runtime knobs are env-overridable via ${VAR:-default}
        self.assertIn('TURNSTILE_THREAD: "${TURNSTILE_THREAD:-1}"', compose)
        self.assertIn('TURNSTILE_DEBUG: "${TURNSTILE_DEBUG:-0}"', compose)
        self.assertIn('TURNSTILE_IDLE_SEC: "${TURNSTILE_IDLE_SEC:-300}"', compose)
        self.assertIn('TURNSTILE_KEEP_BROWSER_ALIVE: "${TURNSTILE_KEEP_BROWSER_ALIVE:-1}"', compose)
        self.assertIn('TURNSTILE_BROWSER_RECYCLE_TASKS: "${TURNSTILE_BROWSER_RECYCLE_TASKS:-8}"', compose)
        self.assertIn('TURNSTILE_BROWSER_RECYCLE_RSS_MB: "${TURNSTILE_BROWSER_RECYCLE_RSS_MB:-700}"', compose)
        self.assertIn('image: ${TURNSTILE_IMAGE:-', compose)

        match = re.search(r'shm_size:\s*"\$\{TURNSTILE_SHM_SIZE:-(?P<size>\d+)(?P<unit>[mMgG])b\}"', compose)
        self.assertIsNotNone(match, "docker-compose.yml must set shm_size")
        size = int(match.group("size"))
        unit = match.group("unit").lower()
        size_mb = size * 1024 if unit == "g" else size
        self.assertLessEqual(size_mb, 512)

    def test_dockerfile_bundles_camoufox_assets(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("FROM python:3.12-slim-bookworm AS python-deps", dockerfile)
        self.assertIn("FROM python:3.12-slim-bookworm AS runtime", dockerfile)
        self.assertIn("COPY --from=python-deps /opt/python /opt/python", dockerfile)
        self.assertIn("ARG TARGETARCH", dockerfile)
        self.assertIn("ARG CAMOUFOX_FETCH_RETRIES=8", dockerfile)
        self.assertIn("ARG CAMOUFOX_MIN_CACHE_MB=500", dockerfile)
        self.assertIn("ARG CAMOUFOX_VERSION=152.0.4", dockerfile)
        self.assertIn("ARG CAMOUFOX_BUILD=beta.28", dockerfile)
        self.assertIn("apt-get install -y --no-install-recommends aria2 ca-certificates", dockerfile)
        self.assertIn("PIP_INDEX_URLS", dockerfile)
        self.assertIn("--mount=type=cache,target=/root/.cache/pip", dockerfile)
        self.assertIn("pypi.tuna.tsinghua.edu.cn/simple", dockerfile)
        self.assertIn("--index-url \"${index_url}\"", dockerfile)
        self.assertIn("--trusted-host \"${host}\"", dockerfile)
        self.assertIn("--prefix=/opt/python", dockerfile)
        self.assertIn("pip install attempt ${attempt} failed on ${index_url}; trying next index", dockerfile)
        self.assertIn("pip install failed on all configured indexes", dockerfile)
        self.assertIn("aria2c", dockerfile)
        self.assertIn("--continue=true", dockerfile)
        self.assertIn("python -m zipfile -t", dockerfile)
        self.assertIn("python -m zipfile -e", dockerfile)
        self.assertIn("browsers/${CAMOUFOX_REPO_NAME}/${full_version}", dockerfile)
        self.assertIn('"active_version": rel', dockerfile)
        self.assertIn("COPY --from=python-deps /root/.cache/camoufox", dockerfile)
        self.assertIn('while [ "$attempt" -le "$CAMOUFOX_FETCH_RETRIES" ]', dockerfile)
        self.assertIn('du -sm "${install_root}"', dockerfile)
        self.assertIn('"$CAMOUFOX_MIN_CACHE_MB"', dockerfile)
        self.assertIn("Camoufox assets were not bundled", dockerfile)
        self.assertNotIn("curl -fsS", dockerfile)
        self.assertNotIn("python -m camoufox fetch", dockerfile)
        self.assertIn("urllib.request", dockerfile)
        self.assertNotIn("rm -rf /root/.cache \\", dockerfile)
        self.assertIn("rm -rf /tmp/*", dockerfile)

    def test_compose_does_not_hide_bundled_camoufox_assets(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertNotIn("/root/.cache/camoufox", compose)
        self.assertNotIn("camoufox-data", compose)

    def test_runtime_decouples_concurrency_from_browser_instances(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_BROWSER_INSTANCES", solver)
        self.assertIn("browser_instance_count", solver)
        self.assertIn("concurrency_slots", solver)
        self.assertIn('TURNSTILE_BROWSER_INSTANCES: "${TURNSTILE_BROWSER_INSTANCES:-1}"', compose)
        self.assertIn("`TURNSTILE_BROWSER_INSTANCES` | `1`", readme)

    def test_runtime_defaults_to_reused_idle_reclaimed_browser_pool(self):
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        entrypoint = (ROOT / "entrypoint.sh").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE", solver)
        self.assertIn("keep_browser_alive", solver)
        self.assertIn("TURNSTILE_LOW_RESOURCE_MODE", solver)
        self.assertIn("low_resource_mode", solver)
        self.assertIn("_reclaim_after_task_if_needed", solver)
        self.assertIn("_tasks_since_recycle", solver)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_TASKS", solver)
        self.assertIn("browser_recycle_tasks", solver)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB", solver)
        self.assertIn("browser_recycle_rss_mb", solver)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC", solver)
        self.assertIn("pool_acquire_timeout_sec", solver)
        self.assertIn("_should_recycle_browser_pool", solver)
        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE=1", dockerfile)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_TASKS=8", dockerfile)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB=700", dockerfile)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120", dockerfile)
        self.assertIn('TURNSTILE_KEEP_BROWSER_ALIVE: "${TURNSTILE_KEEP_BROWSER_ALIVE:-1}"', compose)
        self.assertIn('TURNSTILE_BROWSER_RECYCLE_TASKS: "${TURNSTILE_BROWSER_RECYCLE_TASKS:-8}"', compose)
        self.assertIn('TURNSTILE_BROWSER_RECYCLE_RSS_MB: "${TURNSTILE_BROWSER_RECYCLE_RSS_MB:-700}"', compose)
        self.assertIn('TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC: "${TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC:-120}"', compose)
        self.assertIn('TURNSTILE_LOW_RESOURCE_MODE: "${TURNSTILE_LOW_RESOURCE_MODE:-0}"', compose)
        self.assertNotIn("python -m camoufox fetch", entrypoint)
        self.assertIn("CAMOUFOX_MIN_CACHE_MB", entrypoint)
        self.assertIn('du -sm "${CAMOUFOX_DIR}"', entrypoint)
        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE=1", env_example)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_TASKS=8", env_example)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB=700", env_example)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120", env_example)
        self.assertIn("`TURNSTILE_KEEP_BROWSER_ALIVE` | `1`", readme)
        self.assertIn("`TURNSTILE_BROWSER_RECYCLE_TASKS` | `8`", readme)
        self.assertIn("`TURNSTILE_BROWSER_RECYCLE_RSS_MB` | `700`", readme)
        self.assertIn("`TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC` | `120`", readme)

    def test_busy_browser_timeout_does_not_shutdown_active_task(self):
        solver = (ROOT / "api_solver.py").read_text()
        entrypoint = (ROOT / "entrypoint.sh").read_text()

        self.assertIn("active_solve_count", solver)
        self.assertIn("another task is using the browser", solver)
        self.assertIn("skip pool shutdown", solver)
        self.assertIn('os.getenv("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC", "120")', solver)
        self.assertIn("pool_acquire_timeout", entrypoint)
        self.assertIn("solve_timeout", entrypoint)

    def test_runtime_bounds_pending_work_and_degrades_health_when_stuck(self):
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_MAX_PENDING_TASKS", solver)
        self.assertIn("max_pending_tasks", solver)
        self.assertIn("ERROR_TOO_MANY_TASKS", solver)
        self.assertIn("_task_started_at", solver)
        self.assertIn("TURNSTILE_HEALTH_STUCK_SEC", solver)
        self.assertIn("health_degraded", solver)
        self.assertIn("TURNSTILE_MAX_PENDING_TASKS=2", dockerfile)
        self.assertIn("TURNSTILE_HEALTH_STUCK_SEC=180", dockerfile)
        self.assertIn('TURNSTILE_MAX_PENDING_TASKS: "${TURNSTILE_MAX_PENDING_TASKS:-2}"', compose)
        self.assertIn('TURNSTILE_HEALTH_STUCK_SEC: "${TURNSTILE_HEALTH_STUCK_SEC:-180}"', compose)
        self.assertIn("TURNSTILE_MAX_PENDING_TASKS=2", env_example)
        self.assertIn("TURNSTILE_HEALTH_STUCK_SEC=180", env_example)
        self.assertIn("`TURNSTILE_MAX_PENDING_TASKS` | `2`", readme)
        self.assertIn("`TURNSTILE_HEALTH_STUCK_SEC` | `180`", readme)

    def test_runtime_reports_cgroup_memory_and_cpu_usage(self):
        solver = (ROOT / "api_solver.py").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("_cgroup_resource_report", solver)
        self.assertIn("memory.current", solver)
        self.assertIn("memory.peak", solver)
        self.assertIn("cpu.stat", solver)
        self.assertIn("cgroup_memory_current_mb", solver)
        self.assertIn("cgroup_memory_peak_mb", solver)
        self.assertIn("cgroup_cpu_percent", solver)
        self.assertIn("`cgroup_memory_current_mb`", readme)
        self.assertIn("`cgroup_memory_peak_mb`", readme)
        self.assertIn("`cgroup_cpu_percent`", readme)

    def test_camoufox_does_not_require_default_addon_download(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("from camoufox import DefaultAddons", solver)
        self.assertIn('"exclude_addons": [DefaultAddons.UBO]', solver)

    def test_camoufox_launch_defaults_are_lightweight(self):
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_LOW_RESOURCE_MODE", solver)
        self.assertIn("self.low_resource_mode", solver)
        self.assertIn("TURNSTILE_CAMOUFOX_PROFILE", solver)
        self.assertIn("camoufox_profile", solver)
        self.assertIn("low_resource_mode", solver)
        self.assertIn('"camoufox_launch_options"', solver)
        self.assertIn("low_resource={self.low_resource_mode}", solver)
        self.assertNotIn('"block_images": True', solver)
        self.assertIn('"block_webrtc": True', solver)
        self.assertIn('"disable_coop": True', solver)
        self.assertIn('"enable_cache": False', solver)
        self.assertIn('options["firefox_user_prefs"] = prefs', solver)
        self.assertIn('"dom.ipc.processCount"', solver)
        self.assertIn('"dom.ipc.processCount.webIsolated"', solver)
        self.assertIn('"fission.autostart"', solver)
        self.assertIn('"network.prefetch-next"', solver)
        # Do NOT assert image/WebRender kill-switches — they break Turnstile.
        self.assertNotIn('"permissions.default.image"', solver)
        self.assertNotIn('"gfx.webrender.force-disabled"', solver)
        self.assertIn("Camoufox rejected firefox_user_prefs", solver)
        self.assertIn('camoufox_options.pop("firefox_user_prefs", None)', solver)
        self.assertIn("TURNSTILE_CAMOUFOX_PROFILE=compact", dockerfile)
        self.assertIn('TURNSTILE_CAMOUFOX_PROFILE: "${TURNSTILE_CAMOUFOX_PROFILE:-compact}"', compose)
        self.assertIn("TURNSTILE_CAMOUFOX_PROFILE=compact", env_example)
        self.assertIn("`TURNSTILE_CAMOUFOX_PROFILE` | `compact`", readme)

    def test_runtime_tracks_and_kills_browser_child_processes(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("_snapshot_child_pids", solver)
        self.assertIn("_remember_browser_processes", solver)
        self.assertIn("_kill_browser_process_leftovers", solver)
        self.assertIn("_process_memory_report", solver)
        self.assertIn("browser_process_rss_mb", solver)
        self.assertIn("browser_process_count", solver)
        self.assertIn("children_count", solver)
        self.assertIn("browser_cpu_ticks", solver)
        self.assertIn("resource_report", solver)

    def test_runtime_exposes_worker_queue_and_solve_timeout(self):
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_SOLVE_TIMEOUT_SEC", solver)
        self.assertIn("solve_timeout_sec", solver)
        self.assertIn("_worker_tasks_queued", solver)
        self.assertIn("_worker_tasks_running", solver)
        self.assertIn('"worker_queued"', solver)
        self.assertIn('"worker_running"', solver)
        self.assertIn('"solve_timeout_sec"', solver)
        self.assertIn("TURNSTILE_SOLVE_TIMEOUT_SEC=60", dockerfile)
        self.assertIn('TURNSTILE_SOLVE_TIMEOUT_SEC: "${TURNSTILE_SOLVE_TIMEOUT_SEC:-60}"', compose)
        self.assertIn("TURNSTILE_SOLVE_TIMEOUT_SEC=60", env_example)
        self.assertIn("`TURNSTILE_SOLVE_TIMEOUT_SEC` | `60`", readme)

    def test_runtime_keeps_heavy_page_assets_blocked_by_default(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_UNBLOCK_RENDERING", solver)
        self.assertIn("unblock_rendering", solver)
        self.assertIn('TURNSTILE_UNBLOCK_RENDERING: "${TURNSTILE_UNBLOCK_RENDERING:-0}"', compose)
        self.assertIn("`TURNSTILE_UNBLOCK_RENDERING` | `0`", readme)
        self.assertIn("challenges.cloudflare.com", solver)
        self.assertIn("pool_acquire_timeout", solver)
        self.assertIn("context.close()", solver)

    def test_continuous_call_mode_defaults_to_inline_keep_alive(self):
        """Continuous-call deploy defaults: reuse one browser (inline + keep-alive)."""
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_WORKER_MODE", solver)
        self.assertIn("worker_mode", solver)
        self.assertIn("keep_browser_alive and self.worker_mode == \"process\"", solver)
        self.assertIn("Switching worker_mode from process to inline", solver)
        # Continuous-call defaults (not process isolation).
        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE=1", dockerfile)
        self.assertIn("TURNSTILE_WORKER_MODE=inline", dockerfile)
        self.assertIn("TURNSTILE_WORKER_TIMEOUT=120", dockerfile)
        self.assertIn("TURNSTILE_LOW_RESOURCE_MODE=0", dockerfile)
        self.assertIn('TURNSTILE_KEEP_BROWSER_ALIVE: "${TURNSTILE_KEEP_BROWSER_ALIVE:-1}"', compose)
        self.assertIn('TURNSTILE_WORKER_MODE: "${TURNSTILE_WORKER_MODE:-inline}"', compose)
        self.assertIn('TURNSTILE_WORKER_TIMEOUT: "${TURNSTILE_WORKER_TIMEOUT:-120}"', compose)
        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE=1", env_example)
        self.assertIn("TURNSTILE_WORKER_MODE=inline", env_example)
        self.assertIn("TURNSTILE_WORKER_TIMEOUT=120", env_example)
        self.assertIn("`TURNSTILE_WORKER_MODE` | `inline`", readme)
        # Isolation mode still documented for operators who want min idle RSS.
        self.assertIn("TURNSTILE_WORKER_MODE=process", readme)

    def test_runtime_reclaims_on_rss_and_pool_acquire_timeout(self):
        """Resource guards that stop multi-GB keep-alive drift / stuck pools."""
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB", solver)
        self.assertIn("browser_recycle_rss_mb", solver)
        self.assertIn("_should_recycle_browser_pool", solver)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC", solver)
        self.assertIn("pool_acquire_timeout_sec", solver)
        self.assertIn("asyncio.wait_for", solver)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB=700", dockerfile)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120", dockerfile)
        self.assertIn('TURNSTILE_BROWSER_RECYCLE_RSS_MB: "${TURNSTILE_BROWSER_RECYCLE_RSS_MB:-700}"', compose)
        self.assertIn('TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC: "${TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC:-120}"', compose)
        self.assertIn("TURNSTILE_BROWSER_RECYCLE_RSS_MB=700", env_example)
        self.assertIn("TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC=120", env_example)
        self.assertIn("`TURNSTILE_BROWSER_RECYCLE_RSS_MB` | `700`", readme)
        self.assertIn("`TURNSTILE_POOL_ACQUIRE_TIMEOUT_SEC` | `120`", readme)

    def test_worker_subprocess_entrypoint_exists(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("_solve_turnstile_in_worker", solver)
        self.assertIn("_run_worker_subprocess", solver)
        self.assertIn("--worker-task-json", solver)
        self.assertIn("WORKER_RESULT_PREFIX", solver)
        self.assertIn("_run_worker_from_args", solver)

    def test_turnstile_render_uses_cdata_and_exposes_failure_detail(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("cData:", solver)
        self.assertNotIn("cdata: {cdata_js}", solver)
        self.assertIn("_detect_turnstile_params", solver)
        self.assertIn("__turnstileDiagnostics", solver)
        self.assertIn('errorDescription": error_detail', solver)
        self.assertIn('response["diagnostics"]', solver)


if __name__ == "__main__":
    unittest.main()
