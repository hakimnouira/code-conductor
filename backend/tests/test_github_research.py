import pytest

from app.github_research import build_search_query, should_research, _score


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Build a modern responsive dashboard for my project", True),
        ("Fix the authentication flow and add role-based permissions", True),
        ("Fix typo in README", False),
        ("Explain this line: x = 1", False),
        ("", False),
    ],
)
def test_task_research_classifier(prompt, expected):
    assert should_research(prompt)[0] is expected


def test_design_tasks_get_ui_focused_queries():
    query = build_search_query("Design a beautiful dashboard UI", ["React", "TypeScript"])
    assert "dashboard" in query
    assert "language:TypeScript" in query
    assert "stars:>20" in query


def test_permissive_license_and_recent_updates_improve_score():
    base = {
        "name": "sample",
        "description": "dashboard UI template",
        "stargazers_count": 250,
        "pushed_at": "2026-10-01T00:00:00Z",
        "license": {"spdx_id": "MIT"},
        "archived": False,
    }
    score, reasons = _score(base, "Build a dashboard UI")
    no_license = {**base, "license": None}
    lower_score, _ = _score(no_license, "Build a dashboard UI")
    assert score > lower_score
    assert any("permissive" in reason for reason in reasons)


def test_no_license_is_explicitly_flagged():
    score, reasons = _score({"name": "repo", "stargazers_count": 10}, "build an API")
    assert score >= 0
    assert any("no SPDX license" in reason for reason in reasons)
