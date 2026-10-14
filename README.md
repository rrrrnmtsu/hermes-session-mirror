# session-mirror

Mirror Hermes Desktop / CLI conversations into Discord. One public thread per session in a channel
you pick; each turn posts your message and the reply. Runs on a background thread, so a slow
Discord never delays a turn.

## Setup
```bash
hermes config set plugins.entries.session-mirror.settings.channel_id <CHANNEL_ID>
hermes plugins enable session-mirror
```
The bot needs *Send Messages* + *Create Public Threads* in that channel. Token lookup order:
`agent.secret_scope` → environment (name = `token_secret`, default `DISCORD_BOT_TOKEN`) → optional `token_command` (a command printing the token, e.g. your password-manager CLI; empty = never run).

## Settings (`plugins.entries.session-mirror.settings`)
| key | default | meaning |
|---|---|---|
| `enabled` | `true` | master switch |
| `channel_id` | – | parent channel (required) |
| `platforms` | `desktop,cli,acp` | session sources to mirror (`discord` is never mirrored) |
| `token_secret` | `DISCORD_BOT_TOKEN` | secret/env name for the bot token |
| `token_command` | – | optional command printing the token (fallback) |
| `max_chars` | `6000` | per-side truncation |

## Safety
Text goes through Hermes' `redact_for_egress` plus a local token-pattern net before leaving the
machine. Mentions are disabled (`allowed_mentions.parse=[]`). Tool calls / outputs are not mirrored.
Thread map: `$HERMES_HOME/state/session-mirror.json`. Tests: `python3 -m unittest discover -s tests`.
