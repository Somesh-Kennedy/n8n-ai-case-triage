"""Start a local test stack: mock Salesforce, mock LLM, and n8n (installed under .n8n-runtime) running the workflow.

    cd .n8n-runtime && npm install n8n          # once
    python scripts/run_stack.py                 # leave running; Ctrl-C stops everything this script started
    N8N_E2E=1 pytest tests/test_e2e.py          # in another terminal

To use a real LLM, set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in the environment first and pass --real-llm.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N8N_BIN = ROOT / ".n8n-runtime" / "node_modules" / ".bin" / ("n8n.cmd" if os.name == "nt" else "n8n")


def wait_for(url: str, timeout: float, ok=lambda status: status < 500) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if ok(response.status):
                    return True
        except urllib.error.HTTPError as error:
            if ok(error.code):
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5678)
    parser.add_argument("--real-llm", action="store_true", help="use LLM_BASE_URL/LLM_API_KEY/LLM_MODEL from the environment")
    parser.add_argument("--fresh", action="store_true", help="delete the local n8n data folder first")
    args = parser.parse_args()

    if not N8N_BIN.exists():
        print("n8n is not installed: run `npm install n8n` inside .n8n-runtime first", file=sys.stderr)
        return 2
    data = ROOT / ".n8n-data"
    if args.fresh and data.exists():
        shutil.rmtree(data)
    data.mkdir(exist_ok=True)

    env = {**os.environ,
           "N8N_USER_FOLDER": str(data), "N8N_PORT": str(args.port),
           "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false",           # the workflow reads its settings from environment variables
           "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_VERSION_NOTIFICATIONS_ENABLED": "false",
           "N8N_PERSONALIZATION_ENABLED": "false", "N8N_HIRING_BANNER_ENABLED": "false", "N8N_TEMPLATES_ENABLED": "false",
           # do not keep execution data: it can contain request details, and a test stack has no use for it
           "EXECUTIONS_DATA_SAVE_ON_SUCCESS": "none", "EXECUTIONS_DATA_SAVE_ON_ERROR": "none",
           "SF_INSTANCE_URL": "http://127.0.0.1:8089", "SF_ACCESS_TOKEN": "mock-token", "SF_API_VERSION": "v60.0"}
    if not args.real_llm:
        env.update({"LLM_BASE_URL": "http://127.0.0.1:8090/v1", "LLM_API_KEY": "mock", "LLM_MODEL": "mock-llm"})

    children: list[subprocess.Popen] = []

    def stop(*_):
        for child in children:
            if child.poll() is None:
                if os.name == "nt":  # n8n.cmd is a wrapper: kill the whole process tree, not just the wrapper
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
                else:
                    child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    children.append(subprocess.Popen([sys.executable, "-m", "mock_salesforce.server", "--port", "8089"], cwd=ROOT, env=env))
    if not args.real_llm:
        children.append(subprocess.Popen([sys.executable, "-m", "mock_salesforce.mock_llm", "--port", "8090"], cwd=ROOT, env=env))

    workflow_id = json.loads((ROOT / "workflows" / "case-triage.json").read_text(encoding="utf8"))["id"]
    for step, command in (("importing", ["import:workflow", f"--input={ROOT / 'workflows' / 'case-triage.json'}"]),
                          ("publishing", ["publish:workflow", f"--id={workflow_id}"])):
        print(f"{step} workflow ...", flush=True)
        result = subprocess.run([str(N8N_BIN), *command], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
        print((result.stdout.strip() + " " + result.stderr.strip())[-300:], flush=True)
        if result.returncode != 0:
            print(f"{step} failed", file=sys.stderr)
            stop()

    print("starting n8n ...", flush=True)
    children.append(subprocess.Popen([str(N8N_BIN), "start"], cwd=ROOT, env=env))
    if not wait_for(f"http://localhost:{args.port}/healthz", 180):
        print("n8n did not become healthy", file=sys.stderr)
        stop()
    ready = wait_for(f"http://localhost:{args.port}/webhook/triage", 420, ok=lambda status: status not in (404, 503))
    print(f"stack ready: webhook {'registered' if ready else 'NOT registered (404)'} at http://localhost:{args.port}/webhook/triage", flush=True)
    if not ready:
        stop()
    while all(child.poll() is None for child in children):
        time.sleep(2)
    return 1


if __name__ == "__main__":
    sys.exit(main())
