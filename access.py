"""Per-admin access for hosted mode.

Each Discord admin gets a personal key from the bot's /claude-key command. Only a SHA-256 hash of each
key is stored. Every tool call re-checks that the key's owner is still an Administrator (or the server
owner), so removing someone's admin role cuts their access immediately.
"""

import contextvars
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError

KEYS_FILE = Path(__file__).parent / "admin_keys.json"
KEY_PREFIX = "cbk_"
FORBIDDEN = -32001  # application-defined JSON-RPC error code

# The Discord user behind the current tool call, set by AdminGate (None in local stdio mode)
caller: contextvars.ContextVar[dict | None] = contextvars.ContextVar("caller", default=None)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    def __init__(self, path: Path = KEYS_FILE):
        self.path = path

    def _load(self) -> dict[str, dict]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, keys: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(keys, indent=1))

    def issue(self, user_id: str, username: str) -> str:
        """Create a new key for a user, replacing any key they had."""
        keys = {h: k for h, k in self._load().items() if k["user_id"] != user_id}
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        keys[_hash(key)] = {"user_id": user_id, "username": username, "created": int(time.time())}
        self._save(keys)
        return key

    def lookup(self, key: str) -> dict | None:
        return self._load().get(_hash(key))

    def revoke(self, user_id: str) -> bool:
        keys = self._load()
        kept = {h: k for h, k in keys.items() if k["user_id"] != user_id}
        self._save(kept)
        return len(kept) != len(keys)

    def holders(self) -> list[dict]:
        return list(self._load().values())


class KeyVerifier:
    """MCP bearer-token check: a key is valid if its hash is in the store."""

    def __init__(self, store: KeyStore):
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        entry = self.store.lookup(token)
        if not entry:
            return None
        return AccessToken(token=token, client_id=entry["user_id"], scopes=["admin"],
                           claims={"username": entry["username"]})


class AdminGate:
    """MCP middleware: before each tool call, confirm the caller is still a server admin; afterwards, log changes."""

    def __init__(
        self,
        check_admin: Callable[[str], Awaitable[dict | None]],
        log_change: Callable[[dict, str, dict], Awaitable[None]],
        read_only_tools: set[str],
    ):
        self.check_admin = check_admin
        self.log_change = log_change
        self.read_only_tools = read_only_tools

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)
        token = _request_token(ctx) or get_access_token()
        if token is None:
            raise MCPError(FORBIDDEN, "Not signed in. Get a key with /claude-key in the Discord server.")
        who = await self.check_admin(token.client_id)
        if who is None:
            raise MCPError(FORBIDDEN, "This key's owner is no longer an Administrator of the server.")
        params = ctx.params or {}
        tool, args = params.get("name", "?"), params.get("arguments") or {}
        reset = caller.set(who)
        try:
            result = await call_next(ctx)
        finally:
            caller.reset(reset)
        if tool not in self.read_only_tools and not _is_error(result):
            await self.log_change(who, tool, args)
        return result


def _request_token(ctx: Any) -> AccessToken | None:
    request = getattr(ctx, "request", None)
    user = getattr(request, "scope", {}).get("user") if request is not None else None
    return getattr(user, "access_token", None)


def _is_error(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("isError"))
    return bool(getattr(result, "is_error", False) or getattr(result, "isError", False))
