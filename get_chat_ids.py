#!/usr/bin/env python3
"""Fetch Telegram chat IDs for a bot token and optionally apply to profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib import error as urlerror
from urllib import request as urlrequest


def tg_call(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlrequest.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"Telegram HTTP error {exc.code}: {body}") from exc
    except Exception as exc:
        raise SystemExit(f"Telegram network error: {exc}") from exc

    data = json.loads(raw)
    if not data.get("ok"):
        raise SystemExit(f"Telegram API error: {data}")
    return data


def chat_label(chat: dict) -> str:
    if chat.get("title"):
        return str(chat["title"])
    full = " ".join(
        part for part in [chat.get("first_name"), chat.get("last_name")] if isinstance(part, str) and part.strip()
    ).strip()
    if full:
        return full
    if chat.get("username"):
        return f"@{chat['username']}"
    return "(unknown)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Telegram chat IDs from getUpdates")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent),
        help="Bridge root directory (contains profiles/).",
    )
    parser.add_argument(
        "--profile",
        default="default",
        help="Profile name from profiles/<name>.json (used when --profile-file is not set).",
    )
    parser.add_argument("--token-env", default="TELEGRAM_BOT_TOKEN", help="Env var with bot token")
    parser.add_argument(
        "--profile-file",
        default="",
        help="Profile JSON to patch allowed_chat_ids (overrides --profile).",
    )
    parser.add_argument("--apply-chat-id", type=int, default=None, help="If provided, write this id to profile")
    args = parser.parse_args()

    token = (os.getenv(args.token_env) or "").strip()
    if not token:
        raise SystemExit(f"Env var {args.token_env} is missing")

    updates = tg_call(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}).get("result", [])
    if not isinstance(updates, list):
        updates = []

    chats: dict[int, dict] = {}
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        message = upd.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict):
            continue
        cid = chat.get("id")
        if not isinstance(cid, int):
            continue
        chats[cid] = chat

    if not chats:
        print("No chats found yet. Send a message to your bot first, then run again.")
    else:
        print("Found chats:")
        for cid, chat in sorted(chats.items(), key=lambda x: x[0]):
            print(f"  {cid} | {chat.get('type','?')} | {chat_label(chat)}")

    if args.apply_chat_id is not None:
        if args.profile_file:
            profile_path = Path(args.profile_file)
        else:
            profile_path = Path(args.root).resolve() / "profiles" / f"{args.profile}.json"
        if not profile_path.exists():
            raise SystemExit(f"Profile file not found: {profile_path}")
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        profile["allowed_chat_ids"] = [int(args.apply_chat_id)]
        profile_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(f"Updated profile allowed_chat_ids -> [{int(args.apply_chat_id)}] in {profile_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
