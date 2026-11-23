"""API keys with roles (Phase 1 auth). Phase 2: SSO (OIDC) for agents, mTLS or signed webhooks
for the helpdesk; the endpoints keep the same roles.

    Authorization: Bearer <key>

- Who may call what:
    helpdesk   POST /tickets, GET /tickets/{number}
    agent      GET /tickets (inbox), GET /tickets/{number}, customer-details, decision
    (GET /health is open: Docker's healthcheck)
- .env holds only SHA-256 hashes (`API_KEYS=name:role:hash,...`), so a leaked .env does not
  leak working keys. Keys are 32 random bytes (`copilot keys new <name> <role>`), so a fast hash is
  enough: there is nothing to brute-force (passwords would need bcrypt/argon2; random keys don't).
- No keys configured → every protected endpoint answers 401 (fail closed).
- The agent's name comes from the key, so the audit log records who really looked or decided.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

Role = Literal["helpdesk", "agent"]
ROLES: tuple[Role, ...] = ("helpdesk", "agent")


@dataclass(frozen=True)
class Principal:
    name: str
    role: Role


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


def parse_keys(spec: str) -> dict[str, Principal]:
    """'sara:agent:<sha256>,helpdesk:helpdesk:<sha256>' → {hash: Principal}."""
    out: dict[str, Principal] = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        try:
            name, role, digest = item.split(":")
        except ValueError:
            raise ValueError(f"API_KEYS entry must be name:role:sha256, got {item!r}") from None
        if role not in ROLES:
            raise ValueError(f"API_KEYS: unknown role {role!r} for {name!r} (use {ROLES})")
        if len(digest) != 64:
            raise ValueError(f"API_KEYS: {name!r} needs a 64-char SHA-256 hash, not a raw key")
        out[digest.lower()] = Principal(name, role)  # type: ignore[arg-type]
    return out


_bearer = HTTPBearer(auto_error=False, description="API key: `Authorization: Bearer sk_...`")


def require(*roles: Role):
    """Dependency: the caller must present a valid key with one of these roles."""

    def check(
        request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)
    ) -> Principal:
        if creds is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "missing API key (Authorization: Bearer ...)",
                headers={"WWW-Authenticate": "Bearer"},
            )
        keys: dict[str, Principal] = request.app.state.api_keys
        digest = hash_key(creds.credentials)
        # Constant-time comparison against EVERY stored hash (no early exit): no timing hints.
        principal = None
        for stored, who in keys.items():
            if hmac.compare_digest(stored, digest):
                principal = who
        if principal is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if principal.role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"role {principal.role!r} may not call this endpoint"
            )
        return principal

    return check
