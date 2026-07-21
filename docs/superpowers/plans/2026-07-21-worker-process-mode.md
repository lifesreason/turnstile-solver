# Worker Process Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate Camoufox memory in short-lived worker processes so the main HTTP service remains lightweight after each solve task.

**Architecture:** The main Quart server keeps API state in memory and launches a Python subprocess for each solve when `TURNSTILE_WORKER_MODE=process`. The worker runs the existing Turnstile solve path in-process, emits one JSON result to stdout, and exits; the main process reads that JSON and stores it in the existing `db_results` memory store.

**Tech Stack:** Python 3.12, Quart, asyncio subprocesses, Camoufox, existing unittest-based deployment checks.

---

### Task 1: Lock Worker Mode Configuration

**Files:**
- Modify: `tests/test_deployment_footprint.py`
- Modify: `Dockerfile`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add a failing test**

Add assertions that `TURNSTILE_WORKER_MODE` defaults to `process`, `TURNSTILE_WORKER_TIMEOUT` exists, and README documents both.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_deployment_footprint.DeploymentFootprintTest.test_worker_process_mode_is_default`

Expected: FAIL because the symbols do not exist yet.

- [ ] **Step 3: Add config defaults and docs**

Set `TURNSTILE_WORKER_MODE=process` and `TURNSTILE_WORKER_TIMEOUT=120` in Dockerfile, compose, `.env.example`, and README.

- [ ] **Step 4: Run the test**

Run: `python3 -m unittest discover`

Expected: PASS.

### Task 2: Add Worker Subprocess Execution

**Files:**
- Modify: `api_solver.py`
- Modify: `tests/test_deployment_footprint.py`

- [ ] **Step 1: Add a failing test**

Assert `api_solver.py` contains `_solve_turnstile_in_worker`, `_run_worker_subprocess`, `--worker-task-json`, and `TURNSTILE_WORKER_MODE`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover`

Expected: FAIL before implementation.

- [ ] **Step 3: Implement worker mode**

Add:
- `self.worker_mode`
- `self.worker_timeout`
- `_solve_turnstile_in_worker(...)`
- `_run_worker_subprocess(...)`
- CLI parsing for `--worker-task-json`
- Worker entrypoint that constructs `TurnstileAPIServer` and calls `_solve_turnstile`, then prints result JSON.

- [ ] **Step 4: Route new tasks through worker mode**

In `_enqueue_turnstile`, create `_solve_turnstile_in_worker` task when `worker_mode == "process"`; otherwise keep existing in-process behavior.

- [ ] **Step 5: Run tests**

Run: `python3 -m unittest discover`

Expected: PASS.

### Task 3: Verification

**Files:**
- Verify: all modified files

- [ ] **Step 1: Run static checks**

Run:

```bash
python3 -m unittest discover
PYTHONPYCACHEPREFIX=/private/tmp/turnstile-solver-pycache python3 -m py_compile api_solver.py browser_configs.py db_results.py
docker compose config
bash -n entrypoint.sh
bash -n start.sh
git diff --check
```

Expected: all commands exit 0.
