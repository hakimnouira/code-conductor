import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_workflow_library_create_list_delete_is_persistent(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "workflow-tests.sqlite3"))
    headers = {"Authorization": f"Bearer {get_settings().auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/workflows", headers=headers, json={
            "name": "Fix and verify",
            "prompt": "Inspect the project, fix the reported bug, and run relevant tests.",
            "agent_id": "auto",
            "auto_verify": True,
            "browser_verify_url": "http://localhost:3000",
            "browser_required_text": ["Dashboard", "Tasks"],
            "browser_forbidden_text": ["Application error"],
            "browser_visible_selectors": ["main", "[data-testid='task-list']"],
            "browser_hidden_selectors": ["[role='alert']"],
        })
        assert created.status_code == 200
        item = created.json()
        assert item["name"] == "Fix and verify"
        assert item["auto_verify"] is True
        assert item["browser_verify_url"] == "http://localhost:3000"
        assert item["browser_required_text"] == ["Dashboard", "Tasks"]
        assert item["browser_forbidden_text"] == ["Application error"]
        assert item["browser_visible_selectors"] == ["main", "[data-testid='task-list']"]
        assert item["browser_hidden_selectors"] == ["[role='alert']"]

        listing = await client.get("/workflows", headers=headers)
        assert listing.status_code == 200
        assert any(row["workflow_id"] == item["workflow_id"] for row in listing.json())

        deleted = await client.delete(f"/workflows/{item['workflow_id']}", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        listing_after = await client.get("/workflows", headers=headers)
        assert all(row["workflow_id"] != item["workflow_id"] for row in listing_after.json())


@pytest.mark.asyncio
async def test_workflow_library_requires_authentication():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/workflows")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_workflow_library_rejects_blank_name_and_unsafe_browser_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "workflow-validation.sqlite3"))
    headers = {"Authorization": f"Bearer {get_settings().auth_token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        blank = await client.post("/workflows", headers=headers, json={
            "name": "   ", "prompt": "Do something"
        })
        assert blank.status_code == 422
        unsafe = await client.post("/workflows", headers=headers, json={
            "name": "Browser check", "prompt": "Check page", "browser_verify_url": "file:///etc/passwd"
        })
        assert unsafe.status_code == 422
        credentials = await client.post("/workflows", headers=headers, json={
            "name": "Credential URL", "prompt": "Check page", "browser_verify_url": "https://user:password@example.com"
        })
        assert credentials.status_code == 422


def test_existing_workflow_database_is_migrated_without_losing_presets(tmp_path, monkeypatch):
    import sqlite3
    from app.task_store import list_workflows

    database = tmp_path / "legacy-workflows.sqlite3"
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(database))
    with sqlite3.connect(database) as db:
        db.execute(
            """CREATE TABLE workflow_presets (
                workflow_id TEXT PRIMARY KEY, name TEXT NOT NULL, prompt TEXT NOT NULL,
                agent_id TEXT NOT NULL, auto_verify INTEGER NOT NULL DEFAULT 0,
                browser_verify_url TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            )"""
        )
        db.execute(
            "INSERT INTO workflow_presets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-workflow", "Legacy", "Keep this preset", "auto", 0, "", 1.0, 1.0),
        )
    rows = list_workflows()
    assert len(rows) == 1
    assert rows[0]["workflow_id"] == "legacy-workflow"
    assert rows[0]["browser_required_text"] == []
    assert rows[0]["browser_visible_selectors"] == []
