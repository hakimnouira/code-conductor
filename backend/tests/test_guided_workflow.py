"""Unit tests for the 4-phase Guided Workflow pipeline."""
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.core.config import get_settings

client = TestClient(app)
AUTH_HEADERS = {"Authorization": f"Bearer {get_settings().auth_token}"}


def test_guided_workflow_unauthorized():
    res = client.post("/workflow/run", json={"prompt": "Build a dashboard"})
    assert res.status_code == 401


@patch("app.guided_workflow.research_github")
def test_guided_workflow_pipeline_execution(mock_research):
    mock_research.return_value = {
        "status": "complete",
        "query": "dashboard admin template stars:>20",
        "repositories": [
            {
                "full_name": "tremorlabs/tremor",
                "url": "https://github.com/tremorlabs/tremor",
                "score": 92.5,
                "stars": 14000,
                "license": "Apache-2.0",
                "description": "React components for dashboards",
                "local_path": None,
                "reasons": ["strong community adoption", "permissive license"],
            }
        ],
    }

    res = client.post(
        "/workflow/run",
        headers=AUTH_HEADERS,
        json={"prompt": "Build a beautiful project management dashboard", "clone_references": False},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "completed"
    assert data["phase1_research"]["query"] is not None
    assert len(data["phase1_research"]["repositories"]) == 1
    assert len(data["phase3_plan"]) >= 1
    assert data["phase4_verification"]["provenance_report"]["top_reference"] == "tremorlabs/tremor"
