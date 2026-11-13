"""Sample switch login handler (its log line matches the demo storm)."""

import logging

log = logging.getLogger("sw-core-1.auth")


def reject_login(user: str, addr: str, port: int) -> None:
    # Fixed demo wording: real code would not special-case the user name.
    if user == "admin":
        log.error("Failed password for admin from %s port %d", addr, port)
