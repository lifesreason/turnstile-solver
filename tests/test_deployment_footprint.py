import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DeploymentFootprintTest(unittest.TestCase):
    def test_compose_defaults_are_low_memory(self):
        compose = (ROOT / "docker-compose.yml").read_text()

        self.assertIn('TURNSTILE_THREAD: "${TURNSTILE_THREAD:-1}"', compose)
        self.assertIn('TURNSTILE_DEBUG: "${TURNSTILE_DEBUG:-0}"', compose)
        self.assertIn('TURNSTILE_IDLE_SEC: "${TURNSTILE_IDLE_SEC:-60}"', compose)

        match = re.search(r'shm_size:\s*"\$\{TURNSTILE_SHM_SIZE:-(?P<size>\d+)(?P<unit>[mMgG])b\}"', compose)
        self.assertIsNotNone(match, "docker-compose.yml must set shm_size")
        size = int(match.group("size"))
        unit = match.group("unit").lower()
        size_mb = size * 1024 if unit == "g" else size
        self.assertLessEqual(size_mb, 512)

    def test_dockerfile_uses_slim_runtime_stage(self):
        dockerfile = (ROOT / "Dockerfile").read_text()

        self.assertIn("FROM python:3.12-slim-bookworm AS python-deps", dockerfile)
        self.assertIn("FROM python:3.12-slim-bookworm AS runtime", dockerfile)
        self.assertIn("COPY --from=python-deps /opt/python /opt/python", dockerfile)
        self.assertNotIn("python -m camoufox fetch", dockerfile)
        self.assertNotIn("COPY --from=python-deps /root/.local/share/camoufox", dockerfile)
        self.assertNotIn("curl -fsS", dockerfile)
        self.assertIn("urllib.request", dockerfile)
        self.assertNotIn("rm -rf /root/.cache", dockerfile)
        self.assertIn("rm -rf /tmp/*", dockerfile)

    def test_runtime_decouples_concurrency_from_browser_instances(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_BROWSER_INSTANCES", solver)
        self.assertIn("browser_instance_count", solver)
        self.assertIn("concurrency_slots", solver)
        self.assertIn('TURNSTILE_BROWSER_INSTANCES: "${TURNSTILE_BROWSER_INSTANCES:-1}"', compose)
        self.assertIn("`TURNSTILE_BROWSER_INSTANCES` | `1`", readme)

    def test_runtime_defaults_to_no_warm_browser_pool(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        entrypoint = (ROOT / "entrypoint.sh").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_KEEP_BROWSER_ALIVE", solver)
        self.assertIn("keep_browser_alive", solver)
        self.assertIn("_reclaim_after_task_if_needed", solver)
        self.assertIn('TURNSTILE_KEEP_BROWSER_ALIVE: "${TURNSTILE_KEEP_BROWSER_ALIVE:-0}"', compose)
        self.assertIn("python -m camoufox fetch", entrypoint)
        self.assertIn("`TURNSTILE_KEEP_BROWSER_ALIVE` | `0`", readme)

    def test_runtime_tracks_and_kills_browser_child_processes(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("_snapshot_child_pids", solver)
        self.assertIn("_remember_browser_processes", solver)
        self.assertIn("_kill_browser_process_leftovers", solver)
        self.assertIn("_process_memory_report", solver)
        self.assertIn("browser_process_rss_mb", solver)

    def test_runtime_keeps_heavy_page_assets_blocked_by_default(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_UNBLOCK_RENDERING", solver)
        self.assertIn("unblock_rendering", solver)
        self.assertIn('TURNSTILE_UNBLOCK_RENDERING: "${TURNSTILE_UNBLOCK_RENDERING:-0}"', compose)
        self.assertIn("`TURNSTILE_UNBLOCK_RENDERING` | `0`", readme)

    def test_worker_process_mode_is_default(self):
        solver = (ROOT / "api_solver.py").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        env_example = (ROOT / ".env.example").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_WORKER_MODE", solver)
        self.assertIn("worker_mode", solver)
        self.assertIn("TURNSTILE_WORKER_MODE=process", dockerfile)
        self.assertIn("TURNSTILE_WORKER_TIMEOUT=120", dockerfile)
        self.assertIn('TURNSTILE_WORKER_MODE: "${TURNSTILE_WORKER_MODE:-process}"', compose)
        self.assertIn('TURNSTILE_WORKER_TIMEOUT: "${TURNSTILE_WORKER_TIMEOUT:-120}"', compose)
        self.assertIn("TURNSTILE_WORKER_MODE=process", env_example)
        self.assertIn("TURNSTILE_WORKER_TIMEOUT=120", env_example)
        self.assertIn("`TURNSTILE_WORKER_MODE` | `process`", readme)

    def test_worker_subprocess_entrypoint_exists(self):
        solver = (ROOT / "api_solver.py").read_text()

        self.assertIn("_solve_turnstile_in_worker", solver)
        self.assertIn("_run_worker_subprocess", solver)
        self.assertIn("--worker-task-json", solver)
        self.assertIn("WORKER_RESULT_PREFIX", solver)
        self.assertIn("_run_worker_from_args", solver)


if __name__ == "__main__":
    unittest.main()
