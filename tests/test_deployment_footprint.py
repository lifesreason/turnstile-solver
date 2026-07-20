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
        self.assertIn("COPY --from=python-deps /root/.local/share/camoufox", dockerfile)
        self.assertNotIn("curl -fsS", dockerfile)
        self.assertIn("urllib.request", dockerfile)
        self.assertIn("rm -rf /root/.cache /tmp/*", dockerfile)

    def test_runtime_decouples_concurrency_from_browser_instances(self):
        solver = (ROOT / "api_solver.py").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        readme = (ROOT / "README.md").read_text()

        self.assertIn("TURNSTILE_BROWSER_INSTANCES", solver)
        self.assertIn("browser_instance_count", solver)
        self.assertIn("concurrency_slots", solver)
        self.assertIn('TURNSTILE_BROWSER_INSTANCES: "${TURNSTILE_BROWSER_INSTANCES:-1}"', compose)
        self.assertIn("`TURNSTILE_BROWSER_INSTANCES` | `1`", readme)


if __name__ == "__main__":
    unittest.main()
