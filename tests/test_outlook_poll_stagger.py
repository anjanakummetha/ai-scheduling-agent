"""The backup poll alternates inbox/sentitems across cycles instead of
hitting both in the same cycle — Graph's per-mailbox concurrency limit was
tripping when both polls landed alongside webhook GET retries."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from app import orchestrator as orch


@contextmanager
def _composio_key(value: str):
    prev = orch.settings.composio_api_key
    object.__setattr__(orch.settings, "composio_api_key", value)
    try:
        yield
    finally:
        object.__setattr__(orch.settings, "composio_api_key", prev)


def _run_cycles(n: int, lexi_enabled: bool = False) -> list[str]:
    polled: list[str] = []

    def fake_poll(folder, *, window_start, role="read"):
        polled.append(f"{role}:{folder}")
        return 0

    with (
        _composio_key("test-key"),
        patch.object(orch, "_poll_outlook_folder", side_effect=fake_poll),
        patch.object(orch, "_lexi_mailbox_poll_enabled", return_value=lexi_enabled),
    ):
        for _ in range(n):
            orch._poll_outlook_ingress()
    return polled


def test_one_kory_folder_per_cycle_alternating():
    orch._poll_folder_cursor = 0
    assert _run_cycles(4) == [
        "read:inbox",
        "read:sentitems",
        "read:inbox",
        "read:sentitems",
    ]


def test_never_both_kory_folders_in_one_cycle():
    orch._poll_folder_cursor = 0
    assert len(_run_cycles(1)) == 1


def test_lexi_mailbox_poll_rides_along_when_enabled():
    orch._poll_folder_cursor = 0
    assert _run_cycles(1, lexi_enabled=True) == ["read:inbox", "lexi:inbox"]


def test_no_key_means_no_poll_and_no_cursor_advance():
    orch._poll_folder_cursor = 0
    with (
        _composio_key(""),
        patch.object(orch, "_poll_outlook_folder") as poll,
    ):
        assert orch._poll_outlook_ingress() == 0
    poll.assert_not_called()
    assert orch._poll_folder_cursor == 0
