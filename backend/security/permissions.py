"""Capability model for agent tools (spec §16).

The agent is READ-ONLY. That is enforced structurally: a tool declares the
capability it needs, and the executor refuses to run a tool whose capability is
not in the allow-set. Write tools can be *written* before they are *allowed*,
and turning them on is a deliberate config change plus a per-action user
confirmation — never a model decision.
"""

from __future__ import annotations

from enum import Enum
from typing import FrozenSet, Set


class Capability(str, Enum):
    READ_MESSAGES = "read:messages"
    READ_CALENDAR = "read:calendar"
    READ_CONTACTS = "read:contacts"
    READ_MEMORY = "read:memory"
    WRITE_MEMORY = "write:memory"  # user-approved memories only
    SEARCH = "search"

    # Not granted in any milestone yet. Present so tool authors declare intent.
    SEND_EMAIL = "write:send_email"
    MODIFY_EMAIL = "write:modify_email"
    SEND_MESSAGE = "write:send_message"
    CREATE_CALENDAR_EVENT = "write:calendar_create"
    MODIFY_CALENDAR = "write:calendar_modify"


READ_ONLY_CAPABILITIES: FrozenSet[Capability] = frozenset(
    {
        Capability.READ_MESSAGES,
        Capability.READ_CALENDAR,
        Capability.READ_CONTACTS,
        Capability.READ_MEMORY,
        Capability.WRITE_MEMORY,
        Capability.SEARCH,
    }
)

#: Capabilities that must never execute without an explicit, per-action user
#: confirmation in the UI, even once they are granted.
REQUIRES_CONFIRMATION: FrozenSet[Capability] = frozenset(
    {
        Capability.SEND_EMAIL,
        Capability.MODIFY_EMAIL,
        Capability.SEND_MESSAGE,
        Capability.CREATE_CALENDAR_EVENT,
        Capability.MODIFY_CALENDAR,
    }
)


class PermissionDenied(PermissionError):
    def __init__(self, capability: Capability, tool: str) -> None:
        super().__init__(
            f"Tool {tool!r} requires capability {capability.value!r}, which is not granted. "
            "Gary is read-only by design."
        )
        self.capability = capability
        self.tool = tool


class PermissionSet:
    def __init__(self, granted: Set[Capability] | FrozenSet[Capability] | None = None) -> None:
        self._granted: FrozenSet[Capability] = frozenset(
            granted if granted is not None else READ_ONLY_CAPABILITIES
        )

    def has(self, capability: Capability) -> bool:
        return capability in self._granted

    def check(self, capability: Capability, tool_name: str) -> None:
        if capability not in self._granted:
            raise PermissionDenied(capability, tool_name)

    def needs_confirmation(self, capability: Capability) -> bool:
        return capability in REQUIRES_CONFIRMATION

    @property
    def granted(self) -> FrozenSet[Capability]:
        return self._granted


DEFAULT_PERMISSIONS = PermissionSet(READ_ONLY_CAPABILITIES)
