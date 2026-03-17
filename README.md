# Codex Telegram Bridge

Bridge process that links one Codex session/thread with:

- Telegram chat messages
- Local terminal input

It keeps a persistent thread id in `state/` so context survives restarts.

## Files

- `bridge_native.py` - recommended bridge mode (native Codex CLI-like output).
- `profiles/default.json` - portable default profile.
- `state/` - runtime state and lock files.

## Requirements

1. Python 3.10+.
2. `codex` CLI installed and authenticated.
3. Telegram bot token (only when Telegram mode is enabled).

## Quick start (local only)

Run without Telegram:

```bash
python bridge_native.py --profile default --no-telegram --once "Reply with exactly: OK"
```

Run interactive local mode:

```bash
python bridge_native.py --profile default --no-telegram
```

## Telegram mode

Set token environment variable:

- Linux/macOS:

```bash
export TELEGRAM_BOT_TOKEN="123456789:AA...."
```

- PowerShell:

```powershell
setx TELEGRAM_BOT_TOKEN "123456789:AA...."
```

Then run:

```bash
python bridge_native.py --profile default
```

Bot command menu is registered automatically (`setMyCommands`) on startup.
In `telegram_format_mode: "html"`, bridge service messages (status/help/permissions/etc.) are sent in monospace (`<pre>`).
Available Telegram/local slash commands:

- `/help` - show available commands.
- `/status` - bridge runtime status (thread, busy/idle, queue size) + fresh Codex limits usage (via short probe run).
- `/permissions` - effective Codex sandbox/approval/web-search settings (plural command name).
- `/esc` - interrupt active Codex execution (available only during active run).
- `/thread` - show current thread id.
- `/newsession` - reset thread id; next task starts a new thread.
- `/queue` - current queue size.
- `/ping` - health check.

Telegram file upload support:

- `document` and `photo` attachments are downloaded automatically.
- Files are saved locally under `telegram_uploads_dir` (default: `<project_path>/.bridge_uploads`).
- Bridge appends saved file paths to the queued Codex prompt so the agent can read/process them.
- Image attachments are additionally passed to Codex via `--image`, so instructions in caption/text are applied to the image directly.
- Message caption (if present) is used as prompt text; without caption, bridge uses a default prompt to process uploaded files.

Telegram command menu follows execution state:

- While Codex is running: bridge tries to expose only `/esc`.
- While Codex is idle: regular command set is exposed (without `/esc`).
- On graceful bridge shutdown: command menu is cleared so it is obvious the bridge is stopped.

## Profiles

Profile path format: `profiles/<name>.json`

Important keys:

- `project_path` - absolute or relative path. Relative paths are resolved against bridge `--root`.
- `telegram_bot_token_env` - environment variable name with bot token.
- `allowed_chat_ids` - empty list means "allow any chat".
- `codex_bin` - CLI executable name (`codex` recommended).
- `codex_global_args`, `codex_exec_args` - extra Codex args.
- `codex_permissions` - optional Codex sandbox mode per profile: `read-only` | `workspace-write` | `danger-full-access` (value `full-access` is accepted and mapped to `danger-full-access`).
- `codex_approval_policy` - optional approval mode: `untrusted` | `on-failure` | `on-request` | `never`. If omitted and `codex_permissions=danger-full-access`, bridge defaults to `never`.
- `codex_web_search` - optional boolean. Enables CLI `--search` (web tool). If omitted and `codex_permissions=danger-full-access`, bridge defaults to `true`.
- `thread_title` - optional thread title; bridge applies it via `/rename` and prefixes outgoing Telegram messages with `[thread_title]`.
- `telegram_uploads_dir` - optional path for Telegram-uploaded files. Relative paths are resolved from `project_path`.
- `telegram_max_file_bytes` - optional max attachment size (default `26214400` bytes = 25 MB).

Default profile is portable and uses:

- `project_path: "."`
- `telegram_bot_token_env: "TELEGRAM_BOT_TOKEN"`
- `codex_bin: "codex"`

## Helpful commands

Find chat IDs from bot updates:

```bash
python get_chat_ids.py --token-env TELEGRAM_BOT_TOKEN
```

Apply one allowed chat id:

```bash
python get_chat_ids.py --profile default --token-env TELEGRAM_BOT_TOKEN --apply-chat-id 123456789
```

## Windows helper script

Optional launcher:

```powershell
powershell -ExecutionPolicy Bypass -File .\run_datasmart_master.ps1 -Profile default
```

It auto-loads profile from `profiles/<Profile>.json` and prompts for token if needed.
