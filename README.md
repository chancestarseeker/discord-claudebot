# discord-claudebot

An [MCP](https://modelcontextprotocol.io) server that lets [Claude Code](https://claude.com/claude-code)
manage a Discord server through a bot. Ask Claude to "add a voice channel for raids with a 25-person limit"
or "make #announcements read-only for members", and it does it with the bot's permissions.

Each running copy manages **one** Discord server.

- **Local mode:** the server owner runs it on their own machine. Claude Code starts it over stdio.
- **Hosted mode:** other admins connect over HTTPS with a personal key. Each admin gets a key from the bot's
  `/claude-key` slash command. Every action re-checks that they are still an Administrator.

## What it can do

| Area | Tools |
|---|---|
| Overview | `get_server_overview`, `read_audit_log` |
| Channels | `create_channel`, `edit_channel`, `delete_channel`, `reorder_channels` |
| Permissions | `set_channel_permissions`, `remove_channel_permissions`, `sync_channel_with_category` |
| Roles | `create_role`, `edit_role`, `delete_role`, `set_role_order`, `set_role_icon` (needs Boost Level 2) |
| Members | `list_members`, `add_member_role`, `remove_member_role` |
| Emojis | `list_emojis`, `upload_emoji`, `rename_emoji`, `delete_emoji` |
| Messages | `send_message`, `edit_message`, `read_messages` |
| Server | `edit_server_settings` (verification level, notifications, content filter, system channels) |
| Admin keys | `list_admin_keys`, `revoke_admin_key` (server owner only) |

`get_server_overview` returns every role and every channel grouped by category, with permission overwrites
spelled out as names (`VIEW_CHANNEL`, `SEND_MESSAGES`, …). Claude reads it before making changes.

## Safeguards

- **One server only.** Every tool works only on the server in `DISCORD_GUILD_ID`.
- **Deletes need the exact name.** Deleting a channel, role or emoji requires its current name in
  `confirm_name`, so Claude has to look it up and confirm it first.
- **No surprise pings.** Bot messages never ping @everyone, @here or roles unless `allow_pings` is set.
- **Audit trail.** Every change appears in Discord's audit log with the reason "via Claude Code". In hosted
  mode the reason also includes the admin's name.
- **Claude Code asks first.** By default, Claude Code asks before each tool call.
- **Tidy role order.** `set_role_order` renumbers the whole role list, because Discord often gives new roles
  the same position, which breaks role hierarchy.

## Setup

### 1. Create the bot

1. In the [Discord Developer Portal](https://discord.com/developers/applications), create an application.
2. On **Bot**, reset and copy the token, then turn on **Server Members Intent** and **Message Content
   Intent**. Turn off **Public Bot** so only you can invite it. To do that, first set
   **Installation → Install Link** to *None*.
3. On **OAuth2 → URL Generator**, tick the `bot` and `applications.commands` scopes and the
   **Administrator** permission. Open the generated link and add the bot to your server.
4. In **Server Settings → Roles**, drag the bot's role to the top. A bot can only manage roles below its own.
5. Turn on Developer Mode (User Settings → Advanced), right-click your server icon and **Copy Server ID**.

### 2. Install

Requires Python 3.11 or later.

```
git clone https://github.com/chancestarseeker/discord-claudebot.git
cd discord-claudebot
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip install -r requirements.txt
```

### 3. Store the token

Keep the token in an environment variable. Never put it in code, config files or a chat.

```
setx DISCORD_BOT_TOKEN "<token>"                  # Windows (then open a new terminal)
export DISCORD_BOT_TOKEN="<token>"                # macOS/Linux (add to your shell profile)
```

Anyone with the token controls the bot. If it leaks, reset it in the Developer Portal right away.

### 4. Connect Claude Code

```
claude mcp add claudebot --scope user -e DISCORD_GUILD_ID=<server id> -- <path>/.venv/bin/python <path>/server.py
```

On Windows, use `<path>\.venv\Scripts\python.exe <path>\server.py`. Then run `claude mcp list` and look for
`claudebot ✓ Connected`. Start a new Claude Code conversation and ask it to look at your server.

## Configuration

| Variable | Required | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | The bot's token |
| `DISCORD_GUILD_ID` | yes | ID of the server to manage |
| `DISCORD_ADMIN_CHANNEL_ID` | no | Hosted mode posts each remote admin's change and new keys here |
| `CLAUDEBOT_INSTRUCTIONS` | no | Extra guidance for Claude, e.g. "Text channels are named emoji + '┇' + name" |
| `DISCORD_AUDIT_REASON` | no | Audit log reason (default "via Claude Code") |
| `CLAUDEBOT_PUBLIC_URL` | hosted | Public HTTPS address of the hosted server |
| `CLAUDEBOT_HOST` / `CLAUDEBOT_PORT` | hosted | Listen address (default `127.0.0.1:8765`) |

## Hosted mode: letting other admins connect

Don't share the bot token with other admins. Run hosted mode instead:

```
CLAUDEBOT_PUBLIC_URL=https://bot.example.com DISCORD_GUILD_ID=<server id> python server.py --hosted
```

This does three things:
- Serves MCP at `<CLAUDEBOT_PUBLIC_URL>/mcp`.
- Connects the bot to Discord.
- Registers **`/claude-key`** in your server.

Put it behind HTTPS, for example with a [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
or a small cloud host. A quick tunnel with a random URL works for testing, but its address changes on
every restart, which breaks every admin's saved command.

**How access works:**
1. An admin runs `/claude-key`. Only Administrators can see the command, and it re-checks when used. The bot
   replies privately with a personal key and the exact `claude mcp add --transport http …` command to run.
2. Keys are stored as SHA-256 hashes in `admin_keys.json`, never in plain text. Running `/claude-key` again
   replaces the old key.
3. Before every tool call, the server checks that the key's owner is the server owner or still holds
   Administrator. Remove someone's admin role and their access stops immediately. The owner can also use
   `list_admin_keys` and `revoke_admin_key`.
4. Changes are tagged with the admin's name in the audit log and, if configured, posted in
   `DISCORD_ADMIN_CHANNEL_ID`.

In hosted mode, `upload_emoji` and `set_role_icon` take an `image_url`. Local file paths are refused, so
remote admins can't read files on the host.

## Limitations

- **One server per running copy.** Run another copy with a different `DISCORD_GUILD_ID` for a second server.
- **Boost-locked features.** Role icons need Boost Level 2. Custom emoji slots depend on the server's boost
  level.
- **Bots made by others.** Messages posted by other bots (carl-bot, for example) can only be edited by that
  bot. Claude can read them but not change them.

## License

[MIT](LICENSE)
