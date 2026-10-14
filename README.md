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
| `paused` | `false` | quick stop: `true` stops posting (re-read every 60 s) |
| `channel_id` | – | parent channel (required; int or string accepted) |
| `platforms` | `desktop,cli,acp,tui` | platforms to mirror, case-insensitive (`discord` is never mirrored) |
| `token_secret` | `DISCORD_BOT_TOKEN` | secret/env name for the bot token |
| `token_command` | – | optional command printing the token (fallback) |
| `max_chars` | `6000` | per-side truncation; `0` or negative = unlimited |

Quick stop / resume (no restart, picked up within 60 s):
```bash
hermes config set plugins.entries.session-mirror.settings.paused true
```

`hermes config set` stores a numeric `channel_id` as an int, so `hermes plugins validate`/load may warn
`should be str (got int)`. Harmless (the id is coerced to a string); to silence it, quote the id in
`config.yaml` (`channel_id: "123..."`). The schema has no type accepting both int and str, so `str` is kept.

## Thread titles
New threads are named from the Hermes session title (read-only lookup in `$HERMES_HOME/state.db`, redacted).
If there is no title or the DB is unreadable, the core's default name is used.

## Self-healing
A missing `channel_id`/token no longer disables the plugin for the process: it retries every 60 s and
logs one warning per failure episode. `enabled`/`paused` are re-read on the same cadence. A rotated
token is picked up on an HTTP 401 (the token is looked up again). Pending posts are flushed (≤3 s) at exit.
Blank turns (empty user and assistant text) are skipped.

## Privacy
The thread is visible to everyone who can see the parent channel. Use a private channel.

## Safety
Text goes through Hermes' `redact_for_egress` plus a local token-pattern net before leaving the
machine. Mentions are disabled (`allowed_mentions.parse=[]`). Tool calls / outputs are not mirrored.
Thread map: `$HERMES_HOME/state/session-mirror.json` (written with mode 0600). Tests: `python3 -m unittest discover -s tests`.

## Redaction is fail-closed
User text, assistant text and the thread title are redacted with `fail_closed=True`: if Hermes'
`redact_for_egress` cannot be loaded or raises, the text is replaced by `[redaction-unavailable]`
and never sent raw. A turn where nothing safe remains is skipped entirely.

## Platforms: tui and one-shot
Hermes Desktop's embedded terminal and `hermes --tui` report platform `tui`, so it is mirrored by default.
Platform matching is case-insensitive. One-shot runs (`hermes -z`, `hermes chat -q`) report platform `cli`
but session source `oneshot`; they are detected (via `agent.session_source`, falling back to
`state.db`) and never mirrored.

## Mentions
Message content is sent with `allowed_mentions.parse=[]` and `@everyone`/`@here`/user/role mentions are
neutralised, so mirrored text cannot ping anyone.

## Feedback loop & bots
- If the parent channel is in the gateway's `allowed_channels`, use a **dedicated bot** (a different token
  than the gateway's own bot, via `token_secret`). The gateway ignores its own messages and other bots'
  messages unless `allow_bots` is `mentions`/`all`, so keep `allow_bots=none`.
- A human @mentioning the gateway bot inside a mirrored thread pulls the mirrored text into a Discord-side
  session as context. That is a prompt-injection surface: prefer a private channel that is **not** listed
  in `discord.allowed_channels`.

## Latency
The hook is non-blocking apart from (re)building the Mirror, at most once per 60 s: a configured
`token_command` can block up to 10 s (the Hermes hook timeout is 30 s). Posting itself is on a background
thread. When `channel_id`, `token_secret` or `max_chars` changes, a new Mirror is built and the old one
is closed (flushed, worker stopped).
