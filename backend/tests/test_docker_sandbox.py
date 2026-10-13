"""Tests for the opt-in, fail-closed Docker test sandbox."""
import pytest

from app.safety.test_runner import SafeTestRunner, TestRunnerSecurityError


pytestmark = pytest.mark.unit


def test_docker_sandbox_command_is_networkless_and_resource_limited(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX_IMAGE", "python:3.11-slim")
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: "/usr/bin/docker")
    runner = SafeTestRunner(tmp_path)

    command, container_name, docker = runner._docker_sandbox_command(
        "pytest", ["-q"]
    )

    assert docker == "/usr/bin/docker"
    assert container_name.startswith("code-conductor-test-")
    assert command[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert "--network" in command
    assert command[command.index("--network") + 1] == "none"
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--pids-limit=256" in command
    assert "--memory=2g" in command
    assert "--cpus=2" in command
    assert command[-3:] == ["python", "-m", "pytest"] or command[-4:] == ["python", "-m", "pytest", "-q"]
    assert str(tmp_path.resolve()).replace("\\", "/") in command[command.index("--mount") + 1]


def test_docker_sandbox_fails_closed_when_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX", "docker")
    monkeypatch.delenv("CODE_CONDUCTOR_TEST_SANDBOX_IMAGE", raising=False)
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: "/usr/bin/docker")

    with pytest.raises(TestRunnerSecurityError, match="requires CODE_CONDUCTOR_TEST_SANDBOX_IMAGE"):
        import asyncio
        asyncio.run(SafeTestRunner(tmp_path).run_tests("pytest"))


def test_docker_sandbox_rejects_image_option_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TEST_SANDBOX_IMAGE", "--privileged")
    monkeypatch.setattr("app.safety.test_runner.shutil.which", lambda _name: "/usr/bin/docker")

    with pytest.raises(TestRunnerSecurityError, match="valid, prebuilt image"):
        SafeTestRunner(tmp_path)._docker_sandbox_command("python", [])
