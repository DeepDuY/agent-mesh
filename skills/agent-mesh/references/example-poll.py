#!/usr/bin/env python3
"""Example: login, list agents, dispatch a command task via REST, poll until done."""
import os
import time

import httpx

BASE = os.environ.get("AGENT_MESH_BASE_URL", "http://127.0.0.1:8000")
USER = os.environ.get("AGENT_MESH_USER", "admin")
PASSWORD = os.environ.get("AGENT_MESH_PASSWORD", "admin")


def login() -> str:
    r = httpx.post(
        f"{BASE}/api/auth/login",
        json={"username": USER, "password": PASSWORD},
    )
    r.raise_for_status()
    return r.json()["token"]


def dispatch(agent_ref, instruction: str, mode: str = "command", timeout_s: int = 120) -> str:
    r = httpx.post(
        f"{BASE}/api/tasks/dispatch",
        headers=HEADERS,
        json={
            "agent_id": agent_ref,
            "mode": mode,
            "instruction": instruction,
            "timeout_s": timeout_s,
            "workdir": "/tmp",
        },
    )
    r.raise_for_status()
    return r.json()["task_id"]


def wait_for(task_id: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{BASE}/api/tasks/{task_id}/status", headers=HEADERS)
        r.raise_for_status()
        task = r.json().get("task")
        if task and task["status"] in ("completed", "failed", "timed_out"):
            return task
        time.sleep(2)
    raise TimeoutError(f"task {task_id} did not finish in time")


def main():
    global HEADERS
    HEADERS = {"Authorization": f"Bearer {login()}"}

    agents = httpx.get(f"{BASE}/api/agents", headers=HEADERS).json()["agents"]
    online = [a for a in agents if a.get("online")]
    print(f"online agents: {[(a['id'], a['agent_id']) for a in online]}")
    if not online:
        print("no online agents; install one first")
        return
    target = online[0]

    task_id = dispatch(target["id"], "echo hello from agent-mesh && uname -a", mode="command")
    print(f"dispatched {task_id} to agent {target['id']}")
    task = wait_for(task_id)
    print("status:", task["status"])
    result = task.get("result") or {}
    print("stdout:", result.get("stdout_tail"))


if __name__ == "__main__":
    main()
