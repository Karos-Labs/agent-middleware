"""Who may do what in the Configuration API.

Three roles, ordered. A caller holding a role may do anything the roles below
it may do, so a route names the *minimum* it requires rather than enumerating
the roles it accepts -- enumeration is how a new role gets forgotten at one of
fifty-one call sites.

* ``viewer``  -- read anything. Every ``GET``.
* ``editor``  -- the day-to-day: create, update, publish, dispatch.
* ``admin``   -- anything that removes or resurrects a record, or changes
  whether an agent may run at all.

The line between editor and admin is deliberately about *reversibility and
blast radius*, not about how important the endpoint feels. Editing a prompt is
an editor's job even though it changes what a client receives, because it is
one document and it is versioned. Disabling an agent is an admin's, because it
silently stops every client that depends on it and nothing in the run path
reports why.

Roles attach to the CALLER'S IDENTITY, which in this service is a service
account rather than a person: the portal, the engine and the Studio all reach
this API as themselves. So a binding is a statement about which *service* may
do what, and that is what makes the audit trail meaningful -- ``actor`` becomes
the verified identity that made the call instead of a string it chose to send.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """A caller's authority, lowest to highest."""

    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def satisfies(self, minimum: Role) -> bool:
        """Whether this role meets a route's minimum requirement."""

        return self.rank >= minimum.rank


_RANK: dict[Role, int] = {Role.VIEWER: 0, Role.EDITOR: 1, Role.ADMIN: 2}
