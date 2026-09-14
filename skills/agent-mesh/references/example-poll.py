#!/usr/bin/env python3
"""Example agent-mesh client: login, list agents, dispatch, poll, fetch artifacts.

Demonstrates the recommended rhythm from the skill:
  list agents -> dispatch a command task -> poll status/logs -> download artifacts.

Token handling: the session token expires (default 24h). `Client.request`
re-logs in automatically once on a 401. If the username/password themselves are
invalid, it raises so the caller can ask the user for credentials.
"""
import os
import time

import httpx

BASE = os.environ.get("AGENT_MESH_BASE_URL", "http://127.0.0.1:8000")
USER = os.environ.get("AGENT_MESH_USER", "admin")
PASSWORD = os.environ.get("AGENT_MESH_PASSWORD", "admin")


class AuthExpired(Exception):
    """Raised when the stored credentials cannot produce a valid token."""


class Client:
    def __init__(self, base: str, username: str, password: str):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.token = ""
        self.http = httpx.Client(timeout=30.0)

    def login(self) -> None:
        r = self.http.post(
            f"{self.base}/api/auth/login",
            json={"username": self.username, "password": self.password},
        )
        if r.status_code == 401:
            raise AuthExpired(
                "登录失败：请向用户索取正确的用户名/密码（token 可能已过期）"
            )
        r.raise_for_status()
        self.token = r.json()["token"]

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self.token:
            self.login()
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self.token}"
        r = self.http.request(method, f"{self.base}/api{path}", headers=headers, **kwargs)
        if r.status_code == 401:
            # Session token expired -> re-login once, then retry.
            self.login()
            headers["Authorization"] = f"Bearer {self.token}"
            r = self.http.request(method, f"{self.base}/api{path}", headers=headers, **kwargs)
        r.raise_for_status()
        return r


def wait_for(client: Client, task_id: str, timeout: int = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.request("GET", f"/tasks/{task_id}/status").json().get("task")
        if task and task["status"] in ("completed", "failed", "timed_out", "cancelled"):
            return task
        time.sleep(2)
    raise TimeoutError(f"task {task_id} did not finish in time")


def main() -> None:
    client = Client(BASE, USER, PASSWORD)

    agents = client.request("GET", "/agents").json()["agents"]
    online = [a for a in agents if a.get("online")]
    print("online agents:", [(a["id"], a.get("description") or a["agent_id"]) for a in online])
    if not online:
        print("no online agents; install one first")
        return
    target = online[0]

    # Command task: run a shell command on the remote node.
    task_id = client.request(
        "POST",
        "/tasks/dispatch",
        json={
            "agent_id": target["id"],
            "mode": "command",
            "instruction": "echo hello from agent-mesh && uname -a",
            "timeout_s": 120,
        },
    ).json()["task_id"]
    print(f"dispatched {task_id} to agent {target['id']}")

    task = wait_for(client, task_id)
    result = task.get("result") or {}
    print("status:", task["status"])
    print("stdout:", result.get("stdout_tail"))

    # Download any artifacts the task produced.
    for artifact in result.get("artifacts") or []:
        data = client.request(
            "GET", f"/artifacts/{task_id}/{artifact['artifact_id']}"
        ).content
        fname = artifact["filename"]
        with open(fname, "wb") as fh:
            fh.write(data)
        print(f"saved artifact {fname} ({len(data)} bytes)")


if __name__ == "__main__":
    main()
