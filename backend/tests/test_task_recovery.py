from app.task_recovery import should_resume_pending_task


def test_only_unstarted_pending_tasks_with_original_request_are_resumable():
    assert should_resume_pending_task("pending", None, True) is True
    assert should_resume_pending_task("pending", 10.0, True) is False
    assert should_resume_pending_task("pending", None, False) is False


def test_running_and_terminal_tasks_are_never_automatically_replayed():
    assert should_resume_pending_task("running", 10.0, True) is False
    assert should_resume_pending_task("completed", 10.0, True) is False
    assert should_resume_pending_task("failed", 10.0, True) is False
    assert should_resume_pending_task("cancelled", None, True) is False
