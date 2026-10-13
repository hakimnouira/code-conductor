from concurrent.futures import ThreadPoolExecutor

from app.task_leases import claim_task_lease, release_task_lease, renew_task_lease


def test_only_one_worker_can_claim_a_live_task_lease(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))

    def claim(owner):
        return claim_task_lease("task-1", owner, now=1000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ["worker-a", "worker-b"]))

    assert sorted(outcomes) == [False, True]


def test_expired_lease_can_be_reclaimed_after_worker_crash(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))

    assert claim_task_lease("task-1", "dead-worker", ttl_seconds=10, now=1000)
    assert not claim_task_lease("task-1", "new-worker", now=1005)
    assert claim_task_lease("task-1", "new-worker", now=1011)


def test_only_lease_owner_can_renew_or_release(tmp_path, monkeypatch):
    monkeypatch.setenv("CODE_CONDUCTOR_TASK_DB", str(tmp_path / "tasks.sqlite3"))

    assert claim_task_lease("task-1", "worker-a", now=1000)
    assert not renew_task_lease("task-1", "worker-b", now=1001)
    assert not release_task_lease("task-1", "worker-b")
    assert renew_task_lease("task-1", "worker-a", now=1001)
    assert release_task_lease("task-1", "worker-a")
