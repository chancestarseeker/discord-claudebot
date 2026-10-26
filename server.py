"""MCP server that lets Claude Code manage one Discord server through a bot.

Local mode (default): runs over stdio for the server owner on their own machine.
Hosted mode (--hosted): serves HTTP for other admins, who sign in with a personal key from the bot's
/claude-key command. Every tool call re-checks that the caller is still an Administrator.
"""

import asyncio
import base64
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from access import AdminGate, KeyStore, KeyVerifier, caller

API = "https://discord.com/api/v10"
GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
if not GUILD_ID.isdigit():
    sys.exit("Set DISCORD_GUILD_ID to the ID of the Discord server this bot manages")
AUDIT_REASON = os.environ.get("DISCORD_AUDIT_REASON", "via Claude Code")
# Optional: where hosted mode logs remote admins' changes and new keys
ADMIN_CHANNEL_ID = os.environ.get("DISCORD_ADMIN_CHANNEL_ID")
# Optional: server-specific guidance for Claude, such as channel naming conventions
EXTRA_INSTRUCTIONS = os.environ.get("CLAUDEBOT_INSTRUCTIONS", "")

HOSTED = "--hosted" in sys.argv
PORT = int(os.environ.get("CLAUDEBOT_PORT", "8765"))
PUBLIC_URL = os.environ.get("CLAUDEBOT_PUBLIC_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

logging.getLogger("httpx").setLevel(logging.WARNING)

PERMISSIONS = {
    "CREATE_INSTANT_INVITE": 0, "KICK_MEMBERS": 1, "BAN_MEMBERS": 2, "ADMINISTRATOR": 3,
    "MANAGE_CHANNELS": 4, "MANAGE_GUILD": 5, "ADD_REACTIONS": 6, "VIEW_AUDIT_LOG": 7,
    "PRIORITY_SPEAKER": 8, "STREAM": 9, "VIEW_CHANNEL": 10, "SEND_MESSAGES": 11,
    "SEND_TTS_MESSAGES": 12, "MANAGE_MESSAGES": 13, "EMBED_LINKS": 14, "ATTACH_FILES": 15,
    "READ_MESSAGE_HISTORY": 16, "MENTION_EVERYONE": 17, "USE_EXTERNAL_EMOJIS": 18,
    "VIEW_GUILD_INSIGHTS": 19, "CONNECT": 20, "SPEAK": 21, "MUTE_MEMBERS": 22,
    "DEAFEN_MEMBERS": 23, "MOVE_MEMBERS": 24, "USE_VAD": 25, "CHANGE_NICKNAME": 26,
    "MANAGE_NICKNAMES": 27, "MANAGE_ROLES": 28, "MANAGE_WEBHOOKS": 29,
    "MANAGE_GUILD_EXPRESSIONS": 30, "USE_APPLICATION_COMMANDS": 31, "REQUEST_TO_SPEAK": 32,
    "MANAGE_EVENTS": 33, "MANAGE_THREADS": 34, "CREATE_PUBLIC_THREADS": 35,
    "CREATE_PRIVATE_THREADS": 36, "USE_EXTERNAL_STICKERS": 37, "SEND_MESSAGES_IN_THREADS": 38,
    "USE_EMBEDDED_ACTIVITIES": 39, "MODERATE_MEMBERS": 40, "VIEW_CREATOR_MONETIZATION_ANALYTICS": 41,
    "USE_SOUNDBOARD": 42, "CREATE_GUILD_EXPRESSIONS": 43, "CREATE_EVENTS": 44,
    "USE_EXTERNAL_SOUNDS": 45, "SEND_VOICE_MESSAGES": 46, "SEND_POLLS": 49,
    "USE_EXTERNAL_APPS": 50, "PIN_MESSAGES": 51, "BYPASS_SLOWMODE": 52,
}

CHANNEL_TYPES = {"text": 0, "voice": 2, "category": 4, "announcement": 5, "stage": 13, "forum": 15}
CHANNEL_TYPE_NAMES = {v: k for k, v in CHANNEL_TYPES.items()}
VERIFICATION_LEVELS = ["none", "low", "medium", "high", "very_high"]
NOTIFICATION_LEVELS = ["all_messages", "only_mentions"]
CONTENT_FILTERS = ["disabled", "members_without_roles", "all_members"]

ChannelType = Literal["text", "voice", "category", "announcement", "stage", "forum"]

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def load_token() -> str:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token and os.name == "nt":
        # On Windows, setx only reaches processes started afterwards, so fall back to the stored user value
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token = winreg.QueryValueEx(key, "DISCORD_BOT_TOKEN")[0]
        except OSError:
            pass
    if not token:
        raise RuntimeError("Set the DISCORD_BOT_TOKEN environment variable to your bot's token")
    return token


class Discord:
    def __init__(self, token: str):
        self.http = httpx.AsyncClient(
            base_url=API,
            headers={"Authorization": f"Bot {token}", "User-Agent": "DiscordBot (discord-claudebot, 1.0)"},
            timeout=30,
        )

    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        who = caller.get()
        reason = f"{AUDIT_REASON} ({who['username']})" if who else AUDIT_REASON
        headers = {} if method == "GET" else {"X-Audit-Log-Reason": quote(reason)}
        for _ in range(5):
            r = await self.http.request(method, path, json=json, params=params, headers=headers)
            if r.status_code == 429:
                await asyncio.sleep(float(r.json().get("retry_after", 1)))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Discord API {r.status_code} on {method} {path}: {r.text}")
            return r.json() if r.content else None
        raise RuntimeError(f"Discord kept rate-limiting {method} {path}")

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params or None)


_discord: Discord | None = None


def discord() -> Discord:
    global _discord
    if _discord is None:
        _discord = Discord(load_token())
    return _discord


def perm_bits(names: list[str]) -> str:
    bits = 0
    for name in names:
        key = name.upper().replace(" ", "_")
        if key not in PERMISSIONS:
            raise ValueError(f"Unknown permission {name!r}. Known: {', '.join(PERMISSIONS)}")
        bits |= 1 << PERMISSIONS[key]
    return str(bits)


def perm_names(bits: str | int) -> list[str]:
    bits = int(bits)
    if bits >> PERMISSIONS["ADMINISTRATOR"] & 1:
        return ["ADMINISTRATOR (grants everything)"]
    return [name for name, bit in PERMISSIONS.items() if bits >> bit & 1]


def parse_color(color: str) -> int:
    return int(color.lstrip("#"), 16)


def channel_sort_key(c: dict) -> tuple:
    # Discord lists voice-like channels after text-like ones within a category
    return (c["type"] in (2, 13), c["position"], int(c["id"]))


def compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


async def guild_channel(channel_id: str) -> dict:
    channel = await discord().get(f"/channels/{channel_id}")
    if channel.get("guild_id") != GUILD_ID:
        raise ValueError(f"Channel {channel_id} is not in this server")
    return channel


async def guild_role(role_id: str) -> dict:
    roles = await discord().get(f"/guilds/{GUILD_ID}/roles")
    for role in roles:
        if role["id"] == role_id:
            return role
    raise ValueError(f"Role {role_id} is not in this server")


EMOJI_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
IMAGE_LIMIT = 256 * 1024


async def load_image(image_path: str | None, image_url: str | None) -> str:
    """An image from a local file (local mode only) or a URL, as the data URI Discord expects."""
    if image_url:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            r = await http.get(image_url)
        r.raise_for_status()
        mime = r.headers.get("content-type", "").split(";")[0]
        if mime not in EMOJI_TYPES.values():
            raise ValueError(f"The URL returned {mime or 'no type'}, not a PNG, JPG, GIF or WebP image")
        data = r.content
    elif image_path:
        if HOSTED:
            raise ValueError("Local file paths aren't available when connected remotely; pass image_url instead")
        path = Path(image_path)
        mime = EMOJI_TYPES.get(path.suffix.lower())
        if not mime:
            raise ValueError(f"Unsupported image type {path.suffix!r}; use PNG, JPG, GIF or WebP")
        data = path.read_bytes()
    else:
        raise ValueError("Pass image_path or image_url")
    if len(data) > IMAGE_LIMIT:
        raise ValueError(f"The image is {len(data) // 1024} KB; Discord's limit is 256 KB")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


async def check_admin(user_id: str) -> dict | None:
    """The caller's name if they are the server owner or hold Administrator, else None."""
    d = discord()
    guild = await d.get(f"/guilds/{GUILD_ID}")
    try:
        member = await d.get(f"/guilds/{GUILD_ID}/members/{user_id}")
    except RuntimeError:
        return None  # not in the server any more
    roles = {r["id"]: int(r["permissions"]) for r in guild["roles"]}
    perms = roles[GUILD_ID]
    for rid in member["roles"]:
        perms |= roles.get(rid, 0)
    if guild["owner_id"] != user_id and not perms >> PERMISSIONS["ADMINISTRATOR"] & 1:
        return None
    user = member["user"]
    return {"id": user_id, "username": member.get("nick") or user.get("global_name") or user["username"]}


async def post_admin_log(content: str) -> None:
    if ADMIN_CHANNEL_ID:
        await discord().request("POST", f"/channels/{ADMIN_CHANNEL_ID}/messages",
                                json={"content": content, "allowed_mentions": {"parse": []}})


async def log_change(who: dict, tool: str, args: dict) -> None:
    """Post a one-line record of a remote admin's change in the admin channel, if one is configured."""
    detail = ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "confirm_name")
    if len(detail) > 300:
        detail = detail[:300] + "…"
    await post_admin_log(f"🤖 <@{who['id']}> via Claude: `{tool}`" + (f" ({detail})" if detail else ""))


READ_ONLY_TOOLS = {"get_server_overview", "list_members", "list_emojis", "read_messages", "read_audit_log", "list_admin_keys"}
KEYS = KeyStore()


def hosted_settings() -> dict:
    return {
        "token_verifier": KeyVerifier(KEYS),
        # Keys aren't tied to an audience: KeyVerifier only accepts keys this server issued
        "auth": AuthSettings(issuer_url=PUBLIC_URL, resource_server_url=f"{PUBLIC_URL}/mcp", validate_token_resource=False),
        "middleware": [AdminGate(check_admin, log_change, READ_ONLY_TOOLS)],
    }


mcp = MCPServer(
    name="discord-claudebot",
    instructions=(
        "Manages a Discord server through its bot. Call get_server_overview before making changes "
        "and follow the server's existing naming style. Deleting things requires the exact current "
        "name as confirmation, so confirm with the user first. Messages never ping "
        "@everyone/@here/roles unless allow_pings is set. " + EXTRA_INSTRUCTIONS
    ).strip(),
    **(hosted_settings() if HOSTED else {}),
)


@mcp.tool(annotations=READ)
async def get_server_overview() -> dict:
    """Server settings, every role (top to bottom) and every channel grouped by category, with IDs and permission overwrites."""
    d = discord()
    guild = await d.get(f"/guilds/{GUILD_ID}", with_counts="true")
    channels = await d.get(f"/guilds/{GUILD_ID}/channels")
    roles = {r["id"]: r for r in guild["roles"]}

    def summary(c: dict) -> dict:
        out = {"id": c["id"], "name": c["name"], "type": CHANNEL_TYPE_NAMES.get(c["type"], c["type"]), "position": c["position"]}
        for key in ("topic", "user_limit", "rate_limit_per_user", "nsfw"):
            if c.get(key):
                out[key] = c[key]
        overwrites = [
            {
                "target": roles[o["id"]]["name"] if o["type"] == 0 and o["id"] in roles else f"member {o['id']}",
                "target_id": o["id"],
                "allow": perm_names(o["allow"]),
                "deny": perm_names(o["deny"]),
            }
            for o in c.get("permission_overwrites", [])
        ]
        if overwrites:
            out["permission_overwrites"] = overwrites
        return out

    ordered = sorted(channels, key=channel_sort_key)
    categories = []
    for cat in (c for c in ordered if c["type"] == 4):
        entry = summary(cat)
        entry["channels"] = [summary(c) for c in ordered if c.get("parent_id") == cat["id"]]
        categories.append(entry)

    return {
        "server": {
            "id": guild["id"],
            "name": guild["name"],
            "description": guild.get("description"),
            "approximate_members": guild.get("approximate_member_count"),
            "features": guild.get("features"),
            "verification_level": VERIFICATION_LEVELS[guild["verification_level"]],
            "default_notifications": NOTIFICATION_LEVELS[guild["default_message_notifications"]],
            "explicit_content_filter": CONTENT_FILTERS[guild["explicit_content_filter"]],
            "require_2fa_for_moderation": guild["mfa_level"] == 1,
            "rules_channel_id": guild.get("rules_channel_id"),
            "public_updates_channel_id": guild.get("public_updates_channel_id"),
            "system_channel_id": guild.get("system_channel_id"),
            "afk_channel_id": guild.get("afk_channel_id"),
            "afk_timeout_seconds": guild.get("afk_timeout"),
        },
        "roles_top_to_bottom": [
            {
                "id": r["id"],
                "name": r["name"],
                "position": r["position"],
                "color": f"#{r['color']:06x}" if r["color"] else None,
                "hoist": r["hoist"],
                "mentionable": r["mentionable"],
                "managed_by_integration": r["managed"],
                "permissions": perm_names(r["permissions"]),
            }
            for r in sorted(guild["roles"], key=lambda r: (-r["position"], int(r["id"])))
        ],
        "uncategorized_channels": [summary(c) for c in ordered if c["type"] != 4 and not c.get("parent_id")],
        "categories": categories,
    }


@mcp.tool(annotations=WRITE)
async def create_channel(
    name: str,
    type: ChannelType = "text",
    category_id: str | None = None,
    topic: str | None = None,
    position: int | None = None,
    user_limit: int | None = None,
    slowmode_seconds: int | None = None,
    sync_permissions_with_category: bool = True,
) -> dict:
    """Create a channel or category. user_limit applies to voice channels (0 = unlimited)."""
    body: dict[str, Any] = compact({
        "name": name,
        "type": CHANNEL_TYPES[type],
        "parent_id": category_id,
        "topic": topic,
        "position": position,
        "user_limit": user_limit,
        "rate_limit_per_user": slowmode_seconds,
    })
    if category_id and sync_permissions_with_category:
        parent = await guild_channel(category_id)
        body["permission_overwrites"] = parent.get("permission_overwrites", [])
    channel = await discord().request("POST", f"/guilds/{GUILD_ID}/channels", json=body)
    return {"id": channel["id"], "name": channel["name"], "type": type, "category_id": channel.get("parent_id")}


@mcp.tool(annotations=WRITE)
async def edit_channel(
    channel_id: str,
    name: str | None = None,
    topic: str | None = None,
    category_id: str | None = None,
    remove_from_category: bool = False,
    position: int | None = None,
    user_limit: int | None = None,
    slowmode_seconds: int | None = None,
    nsfw: bool | None = None,
) -> dict:
    """Change a channel's settings. Pass topic="" to clear the topic."""
    await guild_channel(channel_id)
    body = compact({
        "name": name,
        "topic": topic,
        "parent_id": category_id,
        "position": position,
        "user_limit": user_limit,
        "rate_limit_per_user": slowmode_seconds,
        "nsfw": nsfw,
    })
    if remove_from_category:
        body["parent_id"] = None
    channel = await discord().request("PATCH", f"/channels/{channel_id}", json=body)
    return {"id": channel["id"], "name": channel["name"], "category_id": channel.get("parent_id")}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_channel(channel_id: str, confirm_name: str) -> dict:
    """Permanently delete a channel or category (messages cannot be recovered). confirm_name must equal its current name."""
    channel = await guild_channel(channel_id)
    if confirm_name != channel["name"]:
        raise ValueError(f"confirm_name does not match; the channel is named {channel['name']!r}")
    await discord().request("DELETE", f"/channels/{channel_id}")
    return {"deleted": channel["name"]}


@mcp.tool(annotations=WRITE)
async def reorder_channels(channel_ids_in_order: list[str], category_id: str | None = None, sync_permissions_with_category: bool = False) -> dict:
    """Put the listed channels into category_id (or no category) in the given order. Pass category IDs alone with no category_id to reorder categories."""
    body = [
        compact({"id": cid, "position": i, "parent_id": category_id, "lock_permissions": sync_permissions_with_category or None})
        for i, cid in enumerate(channel_ids_in_order)
    ]
    await discord().request("PATCH", f"/guilds/{GUILD_ID}/channels", json=body)
    return {"reordered": len(body)}


@mcp.tool(annotations=WRITE)
async def set_channel_permissions(
    channel_id: str,
    target_id: str,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    target_type: Literal["role", "member"] = "role",
) -> dict:
    """Set (replace) one role's or member's permission overwrite on a channel or category.

    Permission names use Discord's flag names, e.g. VIEW_CHANNEL, SEND_MESSAGES, CONNECT.
    The @everyone role's ID equals the server ID. Anything not listed in allow or deny is inherited.
    """
    await guild_channel(channel_id)
    body = {"type": 0 if target_type == "role" else 1, "allow": perm_bits(allow or []), "deny": perm_bits(deny or [])}
    await discord().request("PUT", f"/channels/{channel_id}/permissions/{target_id}", json=body)
    return {"channel_id": channel_id, "target_id": target_id, "allow": allow or [], "deny": deny or []}


@mcp.tool(annotations=WRITE)
async def remove_channel_permissions(channel_id: str, target_id: str) -> dict:
    """Remove a role's or member's permission overwrite from a channel so it inherits again."""
    await guild_channel(channel_id)
    await discord().request("DELETE", f"/channels/{channel_id}/permissions/{target_id}")
    return {"channel_id": channel_id, "removed_overwrite_for": target_id}


@mcp.tool(annotations=WRITE)
async def sync_channel_with_category(channel_id: str) -> dict:
    """Replace a channel's permission overwrites with its category's, the same as Discord's "Sync Now"."""
    channel = await guild_channel(channel_id)
    if not channel.get("parent_id"):
        raise ValueError("Channel is not in a category")
    parent = await guild_channel(channel["parent_id"])
    await discord().request("PATCH", f"/channels/{channel_id}", json={"permission_overwrites": parent.get("permission_overwrites", [])})
    return {"synced": channel["name"], "with_category": parent["name"]}


@mcp.tool(annotations=WRITE)
async def create_role(
    name: str,
    permissions: list[str] | None = None,
    color: str | None = None,
    hoist: bool = False,
    mentionable: bool = False,
) -> dict:
    """Create a role. permissions defaults to none beyond @everyone's, which suits cosmetic or self-assign roles. color is hex like #C41E3A. hoist shows the role separately in the member list."""
    body = compact({
        "name": name,
        "permissions": perm_bits(permissions or []),
        "color": parse_color(color) if color else None,
        "hoist": hoist,
        "mentionable": mentionable,
    })
    role = await discord().request("POST", f"/guilds/{GUILD_ID}/roles", json=body)
    return {"id": role["id"], "name": role["name"], "position": role["position"]}


@mcp.tool(annotations=WRITE)
async def edit_role(
    role_id: str,
    name: str | None = None,
    permissions: list[str] | None = None,
    color: str | None = None,
    hoist: bool | None = None,
    mentionable: bool | None = None,
) -> dict:
    """Change a role. permissions, when given, replaces the role's whole permission set. color="#000000" removes the color."""
    await guild_role(role_id)
    body = compact({
        "name": name,
        "permissions": perm_bits(permissions) if permissions is not None else None,
        "color": parse_color(color) if color else None,
        "hoist": hoist,
        "mentionable": mentionable,
    })
    role = await discord().request("PATCH", f"/guilds/{GUILD_ID}/roles/{role_id}", json=body)
    return {"id": role["id"], "name": role["name"], "permissions": perm_names(role["permissions"])}


@mcp.tool(annotations=WRITE)
async def set_role_icon(
    role_id: str,
    image_path: str | None = None,
    image_url: str | None = None,
    unicode_emoji: str | None = None,
) -> dict:
    """Show an icon next to members' names for a role, from an image (PNG/JPG, 256 KB max, 64x64 ideal) or a standard emoji.

    Requires Boost Level 2. image_path works only in local mode; remote admins pass image_url.
    Pass none of the image arguments to remove the icon.
    """
    guild = await discord().get(f"/guilds/{GUILD_ID}")
    if "ROLE_ICONS" not in guild.get("features", []):
        raise ValueError(f"Role icons need Boost Level 2; the server is at level {guild.get('premium_tier', 0)}")
    await guild_role(role_id)
    body: dict[str, Any] = {"icon": None, "unicode_emoji": None}
    if image_path or image_url:
        body["icon"] = await load_image(image_path, image_url)
    elif unicode_emoji:
        body["unicode_emoji"] = unicode_emoji
    role = await discord().request("PATCH", f"/guilds/{GUILD_ID}/roles/{role_id}", json=body)
    return {"role": role["name"], "icon": bool(role.get("icon")), "unicode_emoji": role.get("unicode_emoji")}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_role(role_id: str, confirm_name: str) -> dict:
    """Permanently delete a role (it is removed from everyone who has it). confirm_name must equal its current name."""
    role = await guild_role(role_id)
    if confirm_name != role["name"]:
        raise ValueError(f"confirm_name does not match; the role is named {role['name']!r}")
    await discord().request("DELETE", f"/guilds/{GUILD_ID}/roles/{role_id}")
    return {"deleted": role["name"]}


@mcp.tool(annotations=WRITE)
async def set_role_order(role_ids_top_to_bottom: list[str]) -> dict:
    """Reorder the listed roles among the slots they already occupy, highest first. The bot can only move roles below its own highest role."""
    roles = {r["id"]: r for r in await discord().get(f"/guilds/{GUILD_ID}/roles") if r["id"] != GUILD_ID}
    missing = [rid for rid in role_ids_top_to_bottom if rid not in roles]
    if missing:
        raise ValueError(f"Not roles in this server: {missing}")
    listed = iter(role_ids_top_to_bottom)
    current = sorted(roles.values(), key=lambda r: (-r["position"], int(r["id"])))
    order = [next(listed) if r["id"] in set(role_ids_top_to_bottom) else r["id"] for r in current]
    # New roles often share a position, so renumber every role rather than only the listed ones
    body = [{"id": rid, "position": len(order) - i} for i, rid in enumerate(order)]
    await discord().request("PATCH", f"/guilds/{GUILD_ID}/roles", json=body)
    return {"order_top_to_bottom": [roles[rid]["name"] for rid in order]}


@mcp.tool(annotations=READ)
async def list_members(limit: int = 100) -> list[dict]:
    """List server members with their roles. Requires the bot's Server Members Intent to be enabled in the Developer Portal."""
    roles = {r["id"]: r["name"] for r in await discord().get(f"/guilds/{GUILD_ID}/roles")}
    members = await discord().get(f"/guilds/{GUILD_ID}/members", limit=min(limit, 1000))
    return [
        {
            "id": m["user"]["id"],
            "username": m["user"]["username"],
            "display_name": m.get("nick") or m["user"].get("global_name"),
            "bot": m["user"].get("bot", False),
            "roles": [roles.get(rid, rid) for rid in m["roles"]],
            "joined_at": m.get("joined_at"),
        }
        for m in members
    ]


@mcp.tool(annotations=WRITE)
async def add_member_role(member_id: str, role_id: str) -> dict:
    """Give a member a role."""
    await discord().request("PUT", f"/guilds/{GUILD_ID}/members/{member_id}/roles/{role_id}")
    return {"member_id": member_id, "added_role_id": role_id}


@mcp.tool(annotations=WRITE)
async def remove_member_role(member_id: str, role_id: str) -> dict:
    """Take a role away from a member."""
    await discord().request("DELETE", f"/guilds/{GUILD_ID}/members/{member_id}/roles/{role_id}")
    return {"member_id": member_id, "removed_role_id": role_id}


@mcp.tool(annotations=READ)
async def list_emojis() -> list[dict]:
    """List the server's custom emojis, with the code to use each one in a message (e.g. <:he_him:123>)."""
    emojis = await discord().get(f"/guilds/{GUILD_ID}/emojis")
    return [
        {"id": e["id"], "name": e["name"], "animated": e.get("animated", False),
         "code": f"<{'a' if e.get('animated') else ''}:{e['name']}:{e['id']}>"}
        for e in emojis
    ]


@mcp.tool(annotations=WRITE)
async def upload_emoji(name: str, image_path: str | None = None, image_url: str | None = None) -> dict:
    """Upload an image as a custom server emoji.

    name: 2-32 letters, numbers or underscores. Image: PNG, JPG, GIF or WebP, 256 KB or less (128x128 is ideal).
    image_path works only in local mode; remote admins pass image_url.
    """
    if not re.fullmatch(r"\w{2,32}", name):
        raise ValueError("Emoji names must be 2-32 letters, numbers or underscores")
    existing = {e["name"] for e in await discord().get(f"/guilds/{GUILD_ID}/emojis")}
    if name in existing:
        raise ValueError(f"The server already has an emoji named :{name}:")
    image = await load_image(image_path, image_url)
    emoji = await discord().request("POST", f"/guilds/{GUILD_ID}/emojis", json={"name": name, "image": image})
    return {"id": emoji["id"], "name": emoji["name"], "code": f"<:{emoji['name']}:{emoji['id']}>"}


@mcp.tool(annotations=WRITE)
async def rename_emoji(emoji_id: str, new_name: str) -> dict:
    """Rename a custom emoji. Its ID stays the same, so reaction roles and messages using it keep working."""
    if not re.fullmatch(r"\w{2,32}", new_name):
        raise ValueError("Emoji names must be 2-32 letters, numbers or underscores")
    emoji = await discord().request("PATCH", f"/guilds/{GUILD_ID}/emojis/{emoji_id}", json={"name": new_name})
    return {"id": emoji["id"], "name": emoji["name"], "code": f"<:{emoji['name']}:{emoji['id']}>"}


@mcp.tool(annotations=DESTRUCTIVE)
async def delete_emoji(emoji_id: str, confirm_name: str) -> dict:
    """Permanently delete a custom emoji; reactions using it disappear. confirm_name must equal its current name."""
    emoji = await discord().get(f"/guilds/{GUILD_ID}/emojis/{emoji_id}")
    if confirm_name != emoji["name"]:
        raise ValueError(f"confirm_name does not match; the emoji is named {emoji['name']!r}")
    await discord().request("DELETE", f"/guilds/{GUILD_ID}/emojis/{emoji_id}")
    return {"deleted": emoji["name"]}


def message_body(content: str | None, embed_title: str | None, embed_description: str | None, embed_color: str | None, allow_pings: bool) -> dict:
    body: dict[str, Any] = compact({"content": content})
    if embed_title or embed_description:
        body["embeds"] = [compact({
            "title": embed_title,
            "description": embed_description,
            "color": parse_color(embed_color) if embed_color else None,
        })]
    body["allowed_mentions"] = {"parse": ["users", "roles", "everyone"] if allow_pings else []}
    return body


@mcp.tool(annotations=WRITE)
async def send_message(
    channel_id: str,
    content: str | None = None,
    embed_title: str | None = None,
    embed_description: str | None = None,
    embed_color: str | None = None,
    allow_pings: bool = False,
) -> dict:
    """Post a message as the bot, with optional embed. Mentions render but don't notify anyone unless allow_pings is true."""
    await guild_channel(channel_id)
    msg = await discord().request("POST", f"/channels/{channel_id}/messages", json=message_body(content, embed_title, embed_description, embed_color, allow_pings))
    return {"message_id": msg["id"], "channel_id": channel_id}


@mcp.tool(annotations=WRITE)
async def edit_message(
    channel_id: str,
    message_id: str,
    content: str | None = None,
    embed_title: str | None = None,
    embed_description: str | None = None,
    embed_color: str | None = None,
) -> dict:
    """Edit a message the bot posted earlier."""
    await guild_channel(channel_id)
    await discord().request("PATCH", f"/channels/{channel_id}/messages/{message_id}", json=message_body(content, embed_title, embed_description, embed_color, False))
    return {"edited": message_id}


@mcp.tool(annotations=READ)
async def read_messages(channel_id: str, limit: int = 20) -> list[dict]:
    """Read the most recent messages in a channel, oldest first. Message text requires the bot's Message Content Intent."""
    await guild_channel(channel_id)
    msgs = await discord().get(f"/channels/{channel_id}/messages", limit=min(limit, 100))
    return [
        {
            "id": m["id"],
            "author": m["author"].get("global_name") or m["author"]["username"],
            "author_is_bot": m["author"].get("bot", False),
            "content": m["content"],
            "embeds": [compact({"title": e.get("title"), "description": e.get("description")}) for e in m.get("embeds", [])],
            "timestamp": m["timestamp"],
            "pinned": m.get("pinned", False),
        }
        for m in reversed(msgs)
    ]


@mcp.tool(annotations=WRITE)
async def edit_server_settings(
    name: str | None = None,
    description: str | None = None,
    verification_level: Literal["none", "low", "medium", "high", "very_high"] | None = None,
    default_notifications: Literal["all_messages", "only_mentions"] | None = None,
    explicit_content_filter: Literal["disabled", "members_without_roles", "all_members"] | None = None,
    system_channel_id: str | None = None,
    rules_channel_id: str | None = None,
    public_updates_channel_id: str | None = None,
    afk_channel_id: str | None = None,
    afk_timeout_seconds: Literal[60, 300, 900, 1800, 3600] | None = None,
) -> dict:
    """Change server-wide settings. Only the fields you pass are changed."""
    body = compact({
        "name": name,
        "description": description,
        "verification_level": VERIFICATION_LEVELS.index(verification_level) if verification_level else None,
        "default_message_notifications": NOTIFICATION_LEVELS.index(default_notifications) if default_notifications else None,
        "explicit_content_filter": CONTENT_FILTERS.index(explicit_content_filter) if explicit_content_filter else None,
        "system_channel_id": system_channel_id,
        "rules_channel_id": rules_channel_id,
        "public_updates_channel_id": public_updates_channel_id,
        "afk_channel_id": afk_channel_id,
        "afk_timeout": afk_timeout_seconds,
    })
    if not body:
        raise ValueError("No settings given")
    await discord().request("PATCH", f"/guilds/{GUILD_ID}", json=body)
    return {"updated": list(body)}


AUDIT_ACTIONS = {
    1: "server_update", 10: "channel_create", 11: "channel_update", 12: "channel_delete",
    13: "channel_overwrite_create", 14: "channel_overwrite_update", 15: "channel_overwrite_delete",
    20: "member_kick", 22: "member_ban_add", 23: "member_ban_remove", 24: "member_update",
    25: "member_role_update", 28: "bot_add", 30: "role_create", 31: "role_update", 32: "role_delete",
    72: "message_delete", 74: "message_pin", 75: "message_unpin", 80: "integration_create",
    140: "automod_rule_create", 141: "automod_rule_update", 142: "automod_rule_delete",
}


@mcp.tool(annotations=READ)
async def read_audit_log(limit: int = 25) -> list[dict]:
    """Recent entries from the server's audit log: who did what, newest first."""
    log = await discord().get(f"/guilds/{GUILD_ID}/audit-logs", limit=min(limit, 100))
    users = {u["id"]: u.get("global_name") or u["username"] for u in log.get("users", [])}
    return [
        compact({
            "action": AUDIT_ACTIONS.get(e["action_type"], e["action_type"]),
            "by": users.get(e.get("user_id"), e.get("user_id")),
            "target_id": e.get("target_id"),
            "reason": e.get("reason"),
            "changes": e.get("changes"),
        })
        for e in log["audit_log_entries"]
    ]


async def require_owner() -> None:
    who = caller.get()
    if who is None:
        return  # local mode runs on the owner's own machine
    guild = await discord().get(f"/guilds/{GUILD_ID}")
    if guild["owner_id"] != who["id"]:
        raise ValueError("Only the server owner can manage admin keys")


@mcp.tool(annotations=READ)
async def list_admin_keys() -> list[dict]:
    """Who holds a personal key for connecting to this server remotely (server owner only)."""
    await require_owner()
    return [
        {"user_id": k["user_id"], "username": k["username"],
         "created": f"<t:{k['created']}:f>", "still_admin": await check_admin(k["user_id"]) is not None}
        for k in KEYS.holders()
    ]


@mcp.tool(annotations=DESTRUCTIVE)
async def revoke_admin_key(user_id: str) -> dict:
    """Cancel someone's personal key so they can no longer connect (server owner only). They can get a new one with /claude-key while they're an admin."""
    await require_owner()
    if not KEYS.revoke(user_id):
        raise ValueError(f"User {user_id} has no key")
    return {"revoked_key_for": user_id}


KEY_MESSAGE = """**Your personal Claude key for {server}** 🔑
Only you can see this message. Treat the key like a password: anyone with it can act as you.

Run this once in a terminal on the computer where you use Claude Code:
```
claude mcp add --transport http claudebot {url}/mcp --header "Authorization: Bearer {key}"
```
Then start a new Claude Code conversation and ask it to manage the server.

• Every change you make is recorded in the audit log under your name{log_note}.
• Access ends automatically if you stop being an admin.
• Running /claude-key again replaces this key; the old one stops working."""


def run_hosted() -> None:
    """Serve MCP over HTTP for remote admins and run the /claude-key slash command."""
    from urllib.parse import urlparse

    import discord as discord_py
    import uvicorn
    from discord import app_commands
    from mcp.server.transport_security import TransportSecuritySettings

    intents = discord_py.Intents.none()
    intents.guilds = True  # not privileged; slash commands need guild state
    client = discord_py.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    guild = discord_py.Object(id=int(GUILD_ID))

    @tree.command(name="claude-key", description="Get your personal key for managing this server with Claude Code (admins only)", guild=guild)
    @app_commands.default_permissions(administrator=True)
    async def claude_key(interaction: discord_py.Interaction) -> None:
        who = await check_admin(str(interaction.user.id))
        if who is None:
            await interaction.response.send_message("Only server admins can get a Claude key.", ephemeral=True)
            return
        key = KEYS.issue(who["id"], who["username"])
        server = interaction.guild.name if interaction.guild else "this server"
        log_note = f" and in <#{ADMIN_CHANNEL_ID}>" if ADMIN_CHANNEL_ID else ""
        await interaction.response.send_message(
            KEY_MESSAGE.format(server=server, url=PUBLIC_URL, key=key, log_note=log_note), ephemeral=True)
        await post_admin_log(f"🔑 <@{who['id']}> got a new Claude key")

    @client.event
    async def on_ready() -> None:
        await tree.sync(guild=guild)
        logging.info("Discord bot ready; /claude-key registered. MCP endpoint: %s/mcp", PUBLIC_URL)

    public_host = urlparse(PUBLIC_URL).netloc
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", public_host],
        allowed_origins=["http://127.0.0.1:*", "http://localhost:*", f"https://{public_host}"],
    )
    app = mcp.streamable_http_app(stateless_http=True, json_response=True, transport_security=security)
    web = uvicorn.Server(uvicorn.Config(app, host=os.environ.get("CLAUDEBOT_HOST", "127.0.0.1"), port=PORT, log_level="info"))

    async def main() -> None:
        async with client:
            await asyncio.gather(web.serve(), client.start(load_token()))

    asyncio.run(main())


if __name__ == "__main__":
    if HOSTED:
        logging.basicConfig(level=logging.INFO)
        run_hosted()
    else:
        mcp.run()
