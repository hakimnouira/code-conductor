import pytest

from app.safety.test_runner import (
    SafeTestRunner,
    TestRunnerSecurityError,
    get_test_execution_diagnostics,
)


def test_host_mode_is_explicitly_reported_as_unisolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "host")
    monkeypatch.delenv("CODE_CONDUCTOR_REQUIRE_TEST_SANDBOX", raising=False)
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: None)

    diagnostics = get_test_execution_diagnostics()

    assert diagnostics["mode"] == "host"
    assert diagnostics["status"] == "host_mode"
    assert diagnostics["sandbox_required"] is False
    assert any("directly on the host" in warning for warning in diagnostics["warnings"])


@pytest.mark.asyncio
async def test_required_sandbox_fails_closed_before_host_test_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "host")
    monkeypatch.setenv("CODE_CONDUCTOR_REQUIRE_TEST_SANDBOX", "true")

    with pytest.raises(TestRunnerSecurityError, match="Test sandbox enforcement is enabled"):
        await SafeTestRunner(tmp_path).run_tests("pytest")


def test_required_sandbox_policy_violation_is_visible_in_diagnostics(monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "host")
    monkeypatch.setenv("CODE_CONDUCTOR_REQUIRE_TEST_SANDBOX", "1")
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: None)

    diagnostics = get_test_execution_diagnostics()

    assert diagnostics["status"] == "policy_violation"
    assert diagnostics["sandbox_required"] is True
    assert diagnostics["sandbox_image_configured"] is False
    assert diagnostics["docker_available"] is False


def test_docker_mode_reports_missing_sandbox_dependencies(monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "docker")
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX_IMAGE", "python:3.11-slim")
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: None)

    diagnostics = get_test_execution_diagnostics()

    assert diagnostics["status"] == "misconfigured"
    assert diagnostics["docker_available"] is False
    assert any("Docker CLI is unavailable" in warning for warning in diagnostics["warnings"])
