import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.main import app
from app.repository_import import _parse_public_github_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/openai/openai", "https://github.com/openai/openai.git"),
        ("https://github.com/openai/openai.git", "https://github.com/openai/openai.git"),
        (" https://github.com/owner/repo/ ", "https://github.com/owner/repo.git"),
    ],
)
def test_parse_public_github_url(url, expected):
    clone_url, repo_name = _parse_public_github_url(url)
    assert clone_url == expected
    assert repo_name == "openai" if "openai" in expected else repo_name == "repo"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/owner/repo",
        "https://github.com.evil.example/owner/repo",
        "https://user:password@github.com/owner/repo",
        "https://github.com/owner/repo?token=secret",
        "https://github.com/owner/repo/issues",
        "git@github.com:owner/repo.git",
        "https://github.com/../repo",
    ],
)
def test_reject_unsafe_github_url(url):
    with pytest.raises(ValueError):
        _parse_public_github_url(url)


@pytest.mark.asyncio
async def test_clone_endpoint_rejects_non_github_url_before_git(auth_headers=None):
    token = get_settings().auth_token
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/workspace/repository/clone",
            headers=headers,
            json={"repository_url": "https://example.com/owner/repo"},
        )
    assert response.status_code == 400
    assert "github.com" in response.json()["detail"]
