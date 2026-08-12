"""Read-only capability enforcement (spec §16)."""

from __future__ import annotations

import pytest

from backend.security.permissions import (
    DEFAULT_PERMISSIONS,
    READ_ONLY_CAPABILITIES,
    Capability,
    PermissionDenied,
    PermissionSet,
)

WRITE_CAPABILITIES = [
    Capability.SEND_EMAIL,
    Capability.MODIFY_EMAIL,
    Capability.SEND_MESSAGE,
    Capability.CREATE_CALENDAR_EVENT,
    Capability.MODIFY_CALENDAR,
]


@pytest.mark.parametrize("cap", WRITE_CAPABILITIES)
def test_write_capabilities_are_denied_by_default(cap):
    assert not DEFAULT_PERMISSIONS.has(cap)
    with pytest.raises(PermissionDenied):
        DEFAULT_PERMISSIONS.check(cap, "some_tool")


@pytest.mark.parametrize("cap", sorted(READ_ONLY_CAPABILITIES, key=lambda c: c.value))
def test_read_capabilities_are_granted(cap):
    DEFAULT_PERMISSIONS.check(cap, "search_messages")


def test_denial_message_names_the_tool_and_capability():
    with pytest.raises(PermissionDenied) as exc:
        DEFAULT_PERMISSIONS.check(Capability.SEND_EMAIL, "send_email")
    assert "send_email" in str(exc.value)
    assert "read-only" in str(exc.value).lower()


@pytest.mark.parametrize("cap", WRITE_CAPABILITIES)
def test_write_capabilities_still_require_confirmation_once_granted(cap):
    """Granting a write capability must not make it silently executable."""
    perms = PermissionSet({cap})
    perms.check(cap, "tool")  # allowed...
    assert perms.needs_confirmation(cap)  # ...but never without a prompt


def test_read_capabilities_do_not_require_confirmation():
    assert not DEFAULT_PERMISSIONS.needs_confirmation(Capability.SEARCH)
