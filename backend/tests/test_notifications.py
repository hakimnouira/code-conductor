"""Opt-in Windows toast notification tests."""
import sys
import types

import pytest

from app.notifications import notify_schedule_outcome


pytestmark = pytest.mark.unit


def test_desktop_notifications_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CODE_CONDUCTOR_DESKTOP_NOTIFICATIONS", raising=False)
    monkeypatch.setattr("app.notifications.sys.platform", "win32")
    assert notify_schedule_outcome("Daily report", "completed") is False


def test_enabled_windows_notification_uses_schedule_outcome(monkeypatch):
    calls = []

    class FakeNotification:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def show(self):
            calls.append({"shown": True})

    fake_module = types.ModuleType("winotify")
    fake_module.Notification = FakeNotification
    monkeypatch.setitem(sys.modules, "winotify", fake_module)
    monkeypatch.setenv("CODE_CONDUCTOR_DESKTOP_NOTIFICATIONS", "1")
    monkeypatch.setattr("app.notifications.sys.platform", "win32")

    assert notify_schedule_outcome("Daily report", "failed", "Tests did not pass") is True
    assert calls[0]["app_id"] == "Code Conductor"
    assert "needs attention" in calls[0]["title"]
    assert "Tests did not pass" in calls[0]["msg"]
    assert calls[1] == {"shown": True}
