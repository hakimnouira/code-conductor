"""Regression tests for test-runner cancellation cleanup."""
import asyncio

import pytest

from app.safety.test_runner import SafeTestRunner


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_cancelling_test_runner_terminates_the_child_process(tmp_path):
    script = tmp_path / "long_running_test.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    runner = SafeTestRunner(tmp_path)
    task = asyncio.create_task(
        runner.run_tests("python", extra_args=[str(script)], timeout_seconds=60)
    )
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
