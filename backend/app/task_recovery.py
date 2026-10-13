"""Conservative restart recovery policy for durable tasks."""
from __future__ import annotations


def should_resume_pending_task(
    status: str,
    started_at: float | None,
    original_request_available: bool,
) -> bool:
    """Only resume work that had not started and still has its immutable request.

    Running work is never replayed automatically because the agent may already have
    performed external side effects before the process crashed.
    """
    return (
        status == "pending"
        and started_at is None
        and original_request_available
    )
