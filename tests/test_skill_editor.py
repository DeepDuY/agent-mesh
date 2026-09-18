"""Skill page editor: create/edit SKILL.md via the API (PUT /api/skills/{name})."""

from __future__ import annotations

import io
import zipfile

ADMIN_API_TOKEN = "admin-api-token-123"
GLOBAL_TOKEN = "mcp-global-token"


def _admin() -> dict:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def _edge() -> dict:
    return {"Authorization": f"Bearer {GLOBAL_TOKEN}"}


def _md(name: str, description: str, body: str = "Instructions.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n"


def _zip_with(name: str, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", _md(name, "orig desc"))
        for path, data in (extra or {}).items():
            zf.writestr(path, data)
    return buf.getvalue()


def _put(client, name: str, content: str):
    return client.put(f"/api/skills/{name}", json={"content": content}, headers=_admin())


def test_create_skill_from_markdown(client):
    content = _md("my-skill", "does a thing")
    r = _put(client, "my-skill", content)
    assert r.status_code == 200, r.text
    assert r.json()["skill"] == {
        "name": "my-skill",
        "description": "does a thing",
        "version": 1,
        "enabled": True,
    }

    listed = client.get("/api/skills", headers=_edge()).json()["skills"]
    assert listed == [{
        "name": "my-skill",
        "description": "does a thing",
        "version": 1,
        "enabled": True,
    }]

    got = client.get("/api/skills/my-skill/content", headers=_admin())
    assert got.status_code == 200
    assert got.json()["content"] == content

    # The downloadable zip still contains a valid SKILL.md at its root.
    dl = client.get("/api/skills/my-skill/download", headers=_edge())
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert "SKILL.md" in zf.namelist()
        assert zf.read("SKILL.md").decode() == content


def test_edit_skill_bumps_version_and_preserves_files(client):
    up = client.post(
        "/api/skills",
        files={"file": ("skill.zip", _zip_with("git-flow", {"references/x.md": b"keep me"}), "application/zip")},
        headers=_admin(),
    )
    assert up.status_code == 200, up.text
    assert up.json()["skill"]["version"] == 1

    r = _put(client, "git-flow", _md("git-flow", "updated desc", body="New body."))
    assert r.status_code == 200, r.text
    assert r.json()["skill"]["version"] == 2
    assert r.json()["skill"]["description"] == "updated desc"

    dl = client.get("/api/skills/git-flow/download", headers=_edge())
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert zf.read("references/x.md") == b"keep me"
        assert "New body." in zf.read("SKILL.md").decode()


def test_edit_requires_content_and_valid_frontmatter(client):
    assert client.get("/api/skills/nope/content", headers=_admin()).status_code == 404

    r = client.put("/api/skills/foo", json={}, headers=_admin())
    assert r.status_code == 400

    # frontmatter name must match the URL name
    r = _put(client, "foo", _md("bar", "d"))
    assert r.status_code == 400
    assert "must match" in r.json()["detail"]

    # description is required
    r = _put(client, "foo", "---\nname: foo\n---\n\nbody\n")
    assert r.status_code == 400
    assert "description" in r.json()["detail"]

    # bad skill name is rejected
    r = _put(client, "Bad Name", _md("Bad Name", "d"))
    assert r.status_code == 400


def test_skill_content_requires_auth(client):
    _put(client, "auth-skill", _md("auth-skill", "d"))
    assert client.get("/api/skills/auth-skill/content").status_code == 401
