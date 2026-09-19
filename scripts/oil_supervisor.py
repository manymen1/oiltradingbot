#!/usr/bin/env python3
"""Foreground WSL/Linux supervisor. No .env loading, subscriptions or execution."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from polybot.oil.config import load_config
from polybot.oil.store import component_lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO / "configs/oil/observe.yaml"))
    parser.add_argument("--duration", type=float, help="Optional bounded smoke-run duration in seconds")
    args = parser.parse_args()
    config = load_config(args.config)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    processes, logs, restarts, next_start = {}, {}, {}, {}
    started = time.monotonic()
    with component_lock(config.root, "supervisor"):
        try:
            for component in ("news", "market", "analysis"):
                logs[component] = (config.root / f"{component}.log").open("a")
                restarts[component], next_start[component] = 0, 0
            while not stopped.is_set():
                if args.duration and time.monotonic() - started >= args.duration:
                    break
                for component in logs:
                    child = processes.get(component)
                    if child is not None and child.poll() is None:
                        continue
                    if child is not None:
                        restarts[component] += 1
                        next_start[component] = time.monotonic() + min(300, 5 * 2 ** min(restarts[component], 6))
                        print(f"{component} exited {child.returncode}; restart {restarts[component]}", flush=True)
                        del processes[component]
                    if time.monotonic() < next_start[component]:
                        continue
                    processes[component] = subprocess.Popen(
                        [sys.executable, "-m", "polybot.oil", "record", "--component", component,
                         "--config", str(config.path)], cwd=REPO, stdout=logs[component], stderr=subprocess.STDOUT,
                        start_new_session=True)
                    print(f"started {component} pid={processes[component].pid}", flush=True)
                stopped.wait(1)
        finally:
            for child in processes.values():
                if child.poll() is None:
                    child.terminate()
            deadline = time.monotonic() + 200
            for child in processes.values():
                try:
                    child.wait(timeout=max(0.1, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            for stream in logs.values():
                stream.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
