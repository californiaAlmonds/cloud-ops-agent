"""Server-side ledger of human approval tokens.

A token is issued only when a human approves a specific remediation proposal. The
guardrailed AWS CLI later consumes the token before running a write command, so a
guessed, reused, expired or invented token can never authorise a change.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import threading
import time
from typing import Callable

DEFAULT_TTL_SECONDS = 300


@dataclass
class _Grant:
    proposal_id: str
    approver: str
    expires_at: float
    remaining: int = 1


class ApprovalLedger:
    """Thread-safe store of single-use, expiring approval tokens."""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._grants: dict[str, _Grant] = {}
        self._lock = threading.Lock()

    def issue(self, proposal_id: str, approver: str, uses: int = 1) -> str:
        """Create a fresh random token bound to one proposal and approver (default: single use)."""
        token = f"appr-{secrets.token_hex(12)}"
        with self._lock:
            self._purge_locked()
            self._grants[token] = _Grant(
                proposal_id=str(proposal_id),
                approver=str(approver),
                expires_at=self._clock() + self._ttl,
                remaining=max(1, int(uses)),
            )
        return token

    def is_valid(self, token: str | None) -> bool:
        """True if the token exists, is unused and has not expired (does not consume it)."""
        if not token:
            return False
        with self._lock:
            grant = self._grants.get(str(token))
            return bool(grant and grant.remaining > 0 and grant.expires_at > self._clock())

    def consume(self, token: str | None) -> bool:
        """Spend one use of a valid token. Returns False if it was invalid, expired or used up."""
        if not token:
            return False
        with self._lock:
            grant = self._grants.get(str(token))
            if grant is None or grant.remaining <= 0 or grant.expires_at <= self._clock():
                return False
            grant.remaining -= 1
            return True

    def describe(self, token: str | None) -> dict[str, str] | None:
        """Return the proposal and approver a token was issued for (None if unknown)."""
        if not token:
            return None
        with self._lock:
            grant = self._grants.get(str(token))
            if grant is None:
                return None
            return {"proposal_id": grant.proposal_id, "approver": grant.approver}

    def _purge_locked(self) -> None:
        now = self._clock()
        stale = [t for t, g in self._grants.items() if g.remaining <= 0 or g.expires_at <= now]
        for token in stale:
            del self._grants[token]
