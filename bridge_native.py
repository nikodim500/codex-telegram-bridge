#!/usr/bin/env python3
"""Codex <-> Telegram bridge with native CLI-like terminal output.

Goals for this variant:
- local terminal output as close as possible to raw `codex exec` output
- Telegram receives final answer only (no intermediate stream)
- same profile/state model as existing bridge
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime
import hashlib
import html
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

MAX_TELEGRAM_TEXT = 3900
SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-fA-F-]{36})", re.IGNORECASE)
ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
CONTEXT_LIMIT_RE = re.compile(r"^context window:\s*(.+)$", re.IGNORECASE)
FIVE_HOUR_LIMIT_RE = re.compile(r"^5h limit:\s*(.+)$", re.IGNORECASE)
WEEKLY_LIMIT_RE = re.compile(r"^weekly limit:\s*(.+)$", re.IGNORECASE)
DEFAULT_TG_SUMMARY_MAX_CHARS = 1400
DEFAULT_TG_SUMMARY_MAX_LINES = 24
DEFAULT_TG_PROGRESS_MIN_INTERVAL_SECONDS = 1.0
ALLOWED_APPROVAL_POLICIES = {"untrusted", "on-failure", "on-request", "never"}


class BridgeError(RuntimeError):
    """Bridge-level fatal configuration/runtime error."""


@dataclass
class Task:
    """A queued prompt coming either from Telegram or local terminal."""

    source: str  # "telegram" | "local" | "once"
    prompt: str
    chat_id: int | None = None
    message_id: int | None = None
    image_paths: list[str] = field(default_factory=list)
    enqueued_at: float = 0.0


def now_ts() -> float:
    return time.time()


def to_int_set(items: list[Any] | None) -> set[int]:
    result: set[int] = set()
    for item in items or []:
        try:
            result.add(int(item))
        except Exception:
            continue
    return result


def split_telegram_text(text: str, max_len: int = MAX_TELEGRAM_TEXT) -> list[str]:
    """Split long message into Telegram-safe chunks preserving line boundaries when possible."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        cut = remaining.rfind("\n", 0, max_len)
        if cut <= 0:
            cut = max_len

        chunk = remaining[:cut].rstrip()
        chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")

    return chunks


def _format_inline_html(escaped_text: str) -> str:
    """Apply lightweight inline markdown formatting over already-escaped text."""

    # Inline code first to avoid bold substitutions inside code.
    escaped_text = re.sub(
        r"`([^`\n]+)`",
        lambda m: f"<code>{m.group(1)}</code>",
        escaped_text,
    )
    escaped_text = re.sub(r"\*\*([^*\n][^\n]*?)\*\*", r"<b>\1</b>", escaped_text)
    escaped_text = re.sub(r"__([^_\n][^\n]*?)__", r"<b>\1</b>", escaped_text)
    return escaped_text


def markdown_to_telegram_html(markdown_text: str) -> str:
    """Convert a safe markdown subset to Telegram HTML parse mode."""

    text = (markdown_text or "").replace("\r\n", "\n")
    if not text.strip():
        return "<i>(empty)</i>"

    parts = re.split(r"(```[\s\S]*?```)", text)
    html_parts: list[str] = []

    for part in parts:
        if not part:
            continue

        if part.startswith("```") and part.endswith("```"):
            match = re.match(r"```([A-Za-z0-9_+\-]*)\n?([\s\S]*?)```$", part)
            if match:
                lang = (match.group(1) or "").strip()
                code = match.group(2) or ""
            else:
                lang = ""
                code = part[3:-3]

            code_escaped = html.escape(code.strip("\n"))
            if lang:
                lang_escaped = html.escape(lang)
                html_parts.append(f'<pre><code class="language-{lang_escaped}">{code_escaped}</code></pre>')
            else:
                html_parts.append(f"<pre><code>{code_escaped}</code></pre>")
            continue

        lines: list[str] = []
        for line in part.split("\n"):
            heading_match = re.match(r"^\s{0,3}#{1,6}\s+(.*)$", line)
            if heading_match:
                heading_text = html.escape(heading_match.group(1).strip())
                lines.append(f"<b>{heading_text}</b>")
                continue

            escaped_line = html.escape(line)
            escaped_line = _format_inline_html(escaped_line)
            lines.append(escaped_line)

        html_parts.append("\n".join(lines))

    result = "".join(html_parts).strip()
    return result or "<i>(empty)</i>"


def compact_for_telegram(
    text: str,
    *,
    max_chars: int,
    max_lines: int,
    force_summary: bool,
) -> tuple[str, bool]:
    """Build concise Telegram-safe summary and signal whether it was compacted."""
    raw = (text or "").replace("\r\n", "\n").strip()
    if not raw:
        return "(empty final response)", False

    short_enough = len(raw) <= max_chars and raw.count("\n") + 1 <= max_lines
    has_code_fence = "```" in raw
    if short_enough and not has_code_fence and not force_summary:
        return raw, False

    # Avoid sending large code/file dumps into Telegram.
    cleaned = re.sub(r"```[\s\S]*?```", "[code omitted]", raw)
    lines = [ln.strip() for ln in cleaned.split("\n")]

    selected: list[str] = []
    heading_or_list = re.compile(r"^(#{1,6}\s+|[-*•]\s+|\d+\.\s+)")
    for line in lines:
        if not line:
            continue
        if heading_or_list.match(line):
            selected.append(line)
        elif len(line) <= 160 and line[-1:] in {".", "!", "?", ":"}:
            selected.append(line)
        elif not force_summary and len(line) <= 120:
            selected.append(line)
        if len(selected) >= max_lines:
            break

    if not selected:
        for line in lines:
            if not line:
                continue
            selected.append(line[:180])
            if len(selected) >= min(max_lines, 12):
                break

    summary = "\n".join(selected).strip()
    summary = re.sub(r"\n{3,}", "\n\n", summary)
    compacted = summary != raw

    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
        compacted = True

    return summary or "(empty final response)", compacted


class Bridge:
    def __init__(
        self,
        *,
        root_dir: Path,
        profile_name: str,
        no_telegram: bool = False,
        verbose: bool = False,
    ) -> None:
        self.root_dir = root_dir
        self.profile_name = profile_name
        self.no_telegram = no_telegram
        self.verbose = verbose

        self.profile = self._load_profile()
        self.profile_id = str(self.profile.get("profile_id") or self.profile_name)
        self.project_path = self._resolve_project_path(self.profile.get("project_path"))

        requested_codex_bin = str(self.profile.get("codex_bin") or "codex")
        self.codex_bin = self._resolve_executable(requested_codex_bin)

        self.codex_global_args: list[str] = [str(x) for x in self.profile.get("codex_global_args", [])]
        self.codex_exec_args: list[str] = [str(x) for x in self.profile.get("codex_exec_args", [])]
        self.codex_exec_prefix_args: list[str] = []
        self.codex_color_mode = str(self.profile.get("codex_color_mode") or "always").strip().lower()
        if self.codex_color_mode not in {"always", "never", "auto"}:
            raise BridgeError("Unsupported codex_color_mode. Use always|never|auto.")
        self.codex_permissions_mode = self._resolve_codex_permissions_mode()
        if self.codex_permissions_mode:
            if self._has_sandbox_arg(self.codex_global_args) or self._has_sandbox_arg(self.codex_exec_args):
                raise BridgeError(
                    "Use either profile key codex_permissions or --sandbox in codex_global_args/codex_exec_args, not both."
                )
            self.codex_exec_prefix_args.extend(["--sandbox", self.codex_permissions_mode])
        self.codex_approval_policy = self._resolve_codex_approval_policy()
        if self.codex_approval_policy:
            if self._has_approval_arg(self.codex_global_args):
                raise BridgeError(
                    "Use either profile key codex_approval_policy or --ask-for-approval in codex_global_args, not both."
                )
            self.codex_global_args.extend(["--ask-for-approval", self.codex_approval_policy])

        self.codex_web_search = self._resolve_codex_web_search()
        if self.codex_web_search and not self._has_search_arg(self.codex_global_args):
            self.codex_global_args.append("--search")

        self.poll_timeout = int(self.profile.get("poll_timeout_seconds", 25))

        # New default: no intermediate Telegram updates.
        self.telegram_intermediate_updates = bool(self.profile.get("telegram_intermediate_updates", False))
        self.telegram_format_mode = str(self.profile.get("telegram_format_mode") or "html").strip().lower()
        if self.telegram_format_mode not in {"html", "plain"}:
            raise BridgeError("Unsupported telegram_format_mode. Use html|plain.")
        self.startup_telegram_message_enabled = bool(self.profile.get("startup_telegram_message_enabled", False))
        self.startup_telegram_message_text = str(
            self.profile.get("startup_telegram_message_text")
            or "Bridge started.\nProfile: {profile_id}\nThread: {thread_id}"
        )
        self.thread_title = " ".join(str(self.profile.get("thread_title") or "").split())
        self.telegram_force_summary = bool(self.profile.get("telegram_force_summary", True))
        self.telegram_summary_max_chars = int(
            self.profile.get("telegram_summary_max_chars", DEFAULT_TG_SUMMARY_MAX_CHARS)
        )
        self.telegram_summary_max_lines = int(
            self.profile.get("telegram_summary_max_lines", DEFAULT_TG_SUMMARY_MAX_LINES)
        )
        self.telegram_progress_min_interval_seconds = float(
            self.profile.get("telegram_progress_min_interval_seconds", DEFAULT_TG_PROGRESS_MIN_INTERVAL_SECONDS)
        )
        self.telegram_max_file_bytes = int(self.profile.get("telegram_max_file_bytes", 25 * 1024 * 1024))
        if self.telegram_summary_max_chars < 300 or self.telegram_summary_max_chars > MAX_TELEGRAM_TEXT:
            raise BridgeError("telegram_summary_max_chars must be within 300..3900")
        if self.telegram_summary_max_lines < 3 or self.telegram_summary_max_lines > 80:
            raise BridgeError("telegram_summary_max_lines must be within 3..80")
        if self.telegram_progress_min_interval_seconds < 0.0 or self.telegram_progress_min_interval_seconds > 10.0:
            raise BridgeError("telegram_progress_min_interval_seconds must be within 0.0..10.0")
        if self.telegram_max_file_bytes < 1024 or self.telegram_max_file_bytes > 200 * 1024 * 1024:
            raise BridgeError("telegram_max_file_bytes must be within 1024..209715200")

        self.allowed_chat_ids = to_int_set(self.profile.get("allowed_chat_ids", []))
        self.telegram_token_env = str(self.profile.get("telegram_bot_token_env") or "TELEGRAM_BOT_TOKEN")
        self.telegram_token = (os.getenv(self.telegram_token_env) or "").strip()

        self.state_dir = self.root_dir / "state"
        self.state_file = self.state_dir / f"{self.profile_id}.state.json"
        self.lock_file = self.state_dir / f"{self.profile_id}.lock"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        codex_home = Path(os.path.expanduser(os.path.expandvars(os.getenv("CODEX_HOME") or "~/.codex")))
        self.codex_sessions_dir = codex_home / "sessions"
        uploads_raw = str(self.profile.get("telegram_uploads_dir") or ".bridge_uploads").strip()
        uploads_path = Path(os.path.expanduser(os.path.expandvars(uploads_raw)))
        if not uploads_path.is_absolute():
            uploads_path = (self.project_path / uploads_path).resolve()
        self.telegram_uploads_dir = uploads_path
        self.telegram_uploads_dir.mkdir(parents=True, exist_ok=True)
        self.telegram_token_lock_file: Path | None = None
        if not self.no_telegram and self.telegram_token:
            token_hash = hashlib.sha256(self.telegram_token.encode("utf-8")).hexdigest()[:16]
            self.telegram_token_lock_file = self.state_dir / f"telegram-token-{token_hash}.lock"

        self.thread_id: str | None = None
        self.thread_title_applied_for: str | None = None
        self.thread_title_applied_value: str | None = None
        self.telegram_offset: int = 0
        self._load_state()

        self._queue: queue.Queue[Task] = queue.Queue()
        self._running = False
        self._busy = False
        self._current_task: Task | None = None
        self._unauthorized_warned: set[int] = set()
        self._lock_fd: int | None = None
        self._telegram_token_lock_fd: int | None = None
        self._proc_lock = threading.Lock()
        self._active_proc: subprocess.Popen[bytes] | None = None
        self._interrupt_requested = False
        self._interrupt_reason = ""
        self._limits_refresh_lock = threading.Lock()
        self._limit_context_window = ""
        self._limit_5h = ""
        self._limit_weekly = ""
        self._telegram_commands_mode: str | None = None

        if not self.project_path.exists():
            raise BridgeError(f"project_path does not exist: {self.project_path}")
        if not self.codex_bin:
            raise BridgeError("codex executable not found in PATH.")
        if not self.no_telegram and not self.telegram_token:
            raise BridgeError(
                f"Environment variable {self.telegram_token_env} is not set. "
                "Set token or run with --no-telegram."
            )

    def _resolve_project_path(self, raw_path: Any) -> Path:
        if raw_path is None:
            raise BridgeError("Profile key 'project_path' is required.")

        expanded = os.path.expanduser(os.path.expandvars(str(raw_path).strip()))
        if not expanded:
            raise BridgeError("Profile key 'project_path' must not be empty.")

        path = Path(expanded)
        if not path.is_absolute():
            path = (self.root_dir / path).resolve()
        else:
            path = path.resolve()
        return path

    def _resolve_executable(self, command_name: str) -> str:
        if not command_name:
            return ""

        direct = shutil.which(command_name)
        if os.name != "nt":
            return direct or command_name

        has_extension = bool(Path(command_name).suffix)
        candidates: list[str] = []
        if has_extension:
            candidates.append(command_name)
        else:
            # Prefer cmd/exe wrappers on Windows to avoid PowerShell execution policy issues.
            candidates.extend([f"{command_name}.cmd", f"{command_name}.exe", f"{command_name}.bat", command_name])

        if direct and Path(direct).suffix.lower() != ".ps1":
            return direct

        for cand in candidates:
            found = shutil.which(cand)
            if found and Path(found).suffix.lower() != ".ps1":
                return found

        appdata = os.getenv("APPDATA")
        if appdata:
            npm_dir = Path(appdata) / "npm"
            for cand in candidates:
                p = npm_dir / cand
                if p.exists() and p.suffix.lower() != ".ps1":
                    return str(p)
            for cand in candidates:
                p = npm_dir / cand
                if p.exists():
                    return str(p)

        if direct:
            return direct

        for cand in candidates:
            found = shutil.which(cand)
            if found:
                return found

        return command_name

    def _resolve_codex_permissions_mode(self) -> str | None:
        if "codex_sandbox_mode" in self.profile:
            raise BridgeError(
                "Profile key codex_sandbox_mode is no longer supported. Use codex_permissions."
            )

        raw = self.profile.get("codex_permissions")
        if raw is None:
            return None

        value = str(raw).strip().lower()
        if not value:
            return None

        aliases = {
            "full-access": "danger-full-access",
            "full_access": "danger-full-access",
            "full access": "danger-full-access",
            "fullaccess": "danger-full-access",
        }
        value = aliases.get(value, value)
        allowed = {"read-only", "workspace-write", "danger-full-access"}
        if value not in allowed:
            raise BridgeError(
                "Unsupported codex_permissions. "
                "Use read-only|workspace-write|danger-full-access (full-access alias supported)."
            )
        return value

    def _resolve_codex_approval_policy(self) -> str | None:
        raw = self.profile.get("codex_approval_policy")
        if raw is None:
            # Bridge is non-interactive by design; for full access keep command execution unblocked.
            if self.codex_permissions_mode == "danger-full-access":
                return "never"
            return None

        value = str(raw).strip().lower()
        if not value:
            return None
        if value not in ALLOWED_APPROVAL_POLICIES:
            raise BridgeError(
                "Unsupported codex_approval_policy. "
                "Use untrusted|on-failure|on-request|never."
            )
        return value

    def _resolve_codex_web_search(self) -> bool:
        raw = self.profile.get("codex_web_search")
        if raw is None:
            # Full-access profiles usually expect internet access for web tool usage.
            return self.codex_permissions_mode == "danger-full-access"

        if isinstance(raw, bool):
            return raw
        value = str(raw).strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off", ""}:
            return False
        raise BridgeError("Unsupported codex_web_search. Use boolean true|false.")

    @staticmethod
    def _has_sandbox_arg(args: list[str]) -> bool:
        for arg in args:
            if arg in {"-s", "--sandbox"} or arg.startswith("--sandbox="):
                return True
        return False

    @staticmethod
    def _has_approval_arg(args: list[str]) -> bool:
        for arg in args:
            if arg in {"-a", "--ask-for-approval"} or arg.startswith("--ask-for-approval="):
                return True
        return False

    @staticmethod
    def _has_search_arg(args: list[str]) -> bool:
        return any(arg == "--search" for arg in args)

    # -------------------------
    # File/profile/state helpers
    # -------------------------
    def _profile_path(self) -> Path:
        return self.root_dir / "profiles" / f"{self.profile_name}.json"

    def _load_profile(self) -> dict[str, Any]:
        path = self._profile_path()
        if not path.exists():
            raise BridgeError(f"Profile not found: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise BridgeError(f"Failed to parse profile JSON {path}: {exc}") from exc

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.thread_id = data.get("thread_id") or None
            self.thread_title_applied_for = data.get("thread_title_applied_for") or None
            self.thread_title_applied_value = data.get("thread_title_applied_value") or None
            self.telegram_offset = int(data.get("telegram_offset") or 0)
        except Exception:
            self.thread_id = None
            self.thread_title_applied_for = None
            self.thread_title_applied_value = None
            self.telegram_offset = 0

    def _save_state(self) -> None:
        payload = {
            "profile_id": self.profile_id,
            "thread_id": self.thread_id,
            "thread_title_applied_for": self.thread_title_applied_for,
            "thread_title_applied_value": self.thread_title_applied_value,
            "telegram_offset": self.telegram_offset,
            "updated_at_epoch": int(time.time()),
        }
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.state_file)

    def _acquire_lock(self) -> None:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(str(self.lock_file), flags)
        except FileExistsError as exc:
            raise BridgeError(
                f"Profile lock exists: {self.lock_file}\n"
                "Another bridge process is already running for this profile."
            ) from exc

        os.write(fd, f"{os.getpid()}".encode("utf-8"))
        self._lock_fd = fd

        if self.no_telegram or not self.telegram_token_lock_file:
            return

        try:
            tg_fd = os.open(str(self.telegram_token_lock_file), flags)
        except FileExistsError as exc:
            self._release_lock()
            raise BridgeError(
                f"Telegram token lock exists: {self.telegram_token_lock_file}\n"
                "Another bridge process is already running with this Telegram bot token."
            ) from exc

        os.write(tg_fd, f"{os.getpid()}".encode("utf-8"))
        self._telegram_token_lock_fd = tg_fd

    def _release_lock(self) -> None:
        try:
            if self._telegram_token_lock_fd is not None:
                os.close(self._telegram_token_lock_fd)
                self._telegram_token_lock_fd = None
            if self.telegram_token_lock_file and self.telegram_token_lock_file.exists():
                self.telegram_token_lock_file.unlink()
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None
            if self.lock_file.exists():
                self.lock_file.unlink()
        except Exception:
            pass

    # -------------------------
    # Logging
    # -------------------------
    def _log(self, message: str) -> None:
        print(f"[bridge] {message}", flush=True)

    def _log_verbose(self, message: str) -> None:
        if self.verbose:
            self._log(message)

    def _telegram_title_prefix(self) -> str:
        if not self.thread_title:
            return ""
        return f"[{self.thread_title}] "

    def _telegram_text_with_prefix(self, text: str) -> str:
        prefix = self._telegram_title_prefix()
        if not prefix:
            return text
        return f"{prefix}{text}"

    # -------------------------
    # Telegram API
    # -------------------------
    def _tg_call(self, method: str, payload: dict[str, Any], timeout: int = 30) -> Any:
        if self.no_telegram:
            return None
        if not self.telegram_token:
            raise BridgeError("Telegram token missing")

        url = f"https://api.telegram.org/bot{self.telegram_token}/{method}"
        req = urlrequest.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=timeout + 5) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise BridgeError(f"Telegram HTTP error {exc.code}: {body}") from exc
        except Exception as exc:
            raise BridgeError(f"Telegram network error: {exc}") from exc

        try:
            data = json.loads(raw)
        except Exception as exc:
            raise BridgeError(f"Telegram invalid JSON response: {raw[:200]}") from exc

        if not data.get("ok"):
            raise BridgeError(f"Telegram API error: {data}")
        return data.get("result")

    def _send_telegram_raw(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        if self.no_telegram:
            return

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        self._tg_call("sendMessage", payload, timeout=20)

    def _send_telegram(self, chat_id: int, text: str, *, with_thread_prefix: bool = True) -> None:
        if self.no_telegram:
            return
        payload = self._telegram_text_with_prefix(text) if with_thread_prefix else text
        for chunk in split_telegram_text(payload):
            self._send_telegram_raw(chat_id, chunk)

    def _send_telegram_system(self, chat_id: int, text: str, *, with_thread_prefix: bool = True) -> None:
        """Send bridge/system info in monospace when HTML mode is enabled."""
        if self.no_telegram:
            return
        payload = self._telegram_text_with_prefix(text) if with_thread_prefix else text
        if self.telegram_format_mode != "html":
            self._send_telegram(chat_id, text, with_thread_prefix=with_thread_prefix)
            return
        html_payload = f"<pre>{html.escape(payload)}</pre>"
        if len(html_payload) > MAX_TELEGRAM_TEXT:
            # Keep delivery robust for larger status/help payloads.
            self._send_telegram(chat_id, payload, with_thread_prefix=False)
            return
        try:
            self._send_telegram_raw(chat_id, html_payload, parse_mode="HTML")
        except Exception:
            self._send_telegram(chat_id, payload, with_thread_prefix=False)

    def _sanitize_upload_name(self, value: str) -> str:
        normalized = re.sub(r"[^\w.\-]+", "_", (value or "").strip(), flags=re.UNICODE)
        normalized = normalized.strip("._-")
        return normalized or "file"

    def _tg_download_file(self, file_id: str) -> tuple[bytes, str]:
        info = self._tg_call("getFile", {"file_id": file_id}, timeout=25)
        if not isinstance(info, dict):
            raise BridgeError("Telegram getFile returned invalid payload")
        file_path = str(info.get("file_path") or "").strip()
        if not file_path:
            raise BridgeError("Telegram getFile returned empty file_path")
        url = f"https://api.telegram.org/file/bot{self.telegram_token}/{file_path}"
        req = urlrequest.Request(url, method="GET")
        try:
            with urlrequest.urlopen(req, timeout=35) as resp:
                data = resp.read()
        except Exception as exc:
            raise BridgeError(f"Telegram file download failed: {exc}") from exc
        return data, file_path

    def _save_telegram_file(
        self,
        *,
        file_id: str,
        original_name: str | None,
        mime_type: str | None,
        chat_id: int,
    ) -> dict[str, Any]:
        data, remote_file_path = self._tg_download_file(file_id)
        size = len(data)
        if size > self.telegram_max_file_bytes:
            raise BridgeError(
                f"File is too large ({size} bytes). Limit: {self.telegram_max_file_bytes} bytes."
            )

        original = (original_name or "").strip()
        suffix = Path(original).suffix if original else ""
        if not suffix:
            suffix = Path(remote_file_path).suffix
        if not suffix and mime_type:
            suffix = mimetypes.guess_extension(mime_type) or ""
        if not suffix:
            suffix = ".bin"

        stem = Path(original).stem if original else f"tg_{file_id[:12]}"
        stem = self._sanitize_upload_name(stem)
        ts = int(time.time() * 1000)
        fname = f"{ts}_{chat_id}_{stem}{suffix}"
        out_path = self.telegram_uploads_dir / fname
        out_path.write_bytes(data)

        return {
            "path": str(out_path),
            "name": original or Path(remote_file_path).name or fname,
            "mime": mime_type or "",
            "size": size,
            "is_image": bool((mime_type or "").lower().startswith("image/")),
        }

    def _extract_telegram_attachments(self, message: dict[str, Any], chat_id: int) -> tuple[list[dict[str, Any]], list[str]]:
        files: list[dict[str, Any]] = []
        warnings: list[str] = []

        document = message.get("document")
        if isinstance(document, dict):
            file_id = str(document.get("file_id") or "").strip()
            if file_id:
                try:
                    files.append(
                        self._save_telegram_file(
                            file_id=file_id,
                            original_name=str(document.get("file_name") or "").strip() or None,
                            mime_type=str(document.get("mime_type") or "").strip() or None,
                            chat_id=chat_id,
                        )
                    )
                except Exception as exc:
                    warnings.append(f"document: {exc}")

        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            best_photo: dict[str, Any] | None = None
            for item in photos:
                if not isinstance(item, dict):
                    continue
                if best_photo is None:
                    best_photo = item
                    continue
                best_size = int(best_photo.get("file_size") or 0)
                item_size = int(item.get("file_size") or 0)
                if item_size >= best_size:
                    best_photo = item
            if best_photo is not None:
                file_id = str(best_photo.get("file_id") or "").strip()
                if file_id:
                    try:
                        files.append(
                            self._save_telegram_file(
                                file_id=file_id,
                                original_name=f"photo_{best_photo.get('file_unique_id') or file_id[:12]}.jpg",
                                mime_type="image/jpeg",
                                chat_id=chat_id,
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"photo: {exc}")

        return files, warnings

    def _build_prompt_from_telegram_message(self, text: str, attachments: list[dict[str, Any]]) -> str:
        clean = (text or "").strip()
        if not attachments:
            return clean

        base_prompt = clean or "Process the uploaded file(s)."
        lines = [
            "Telegram attachments were downloaded to local files:",
        ]
        for item in attachments:
            lines.append(
                f"- {item['path']} (name: {item['name']}; mime: {item['mime'] or 'unknown'}; bytes: {item['size']})"
            )
        if any(bool(item.get("is_image")) for item in attachments):
            lines.append("Image attachments are passed directly to Codex as image inputs.")
        lines.append("Use these exact paths while working on the request.")
        return f"{base_prompt}\n\n" + "\n".join(lines)

    def _build_startup_message(self) -> str:
        data = {
            "profile_id": self.profile_id,
            "project_path": str(self.project_path),
            "thread_id": self.thread_id or "(not started)",
            "thread_title": self.thread_title or "-",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            return self.startup_telegram_message_text.format(**data)
        except Exception:
            return (
                "Bridge started.\n"
                f"Profile: {data['profile_id']}\n"
                f"Thread: {data['thread_id']}\n"
                f"Thread title: {data['thread_title']}\n"
                f"Time: {data['started_at']}"
            )

    def _send_startup_telegram_message(self) -> None:
        if self.no_telegram or not self.startup_telegram_message_enabled:
            return

        targets = sorted(self.allowed_chat_ids)
        if not targets:
            self._log("startup_telegram_message skipped: allowed_chat_ids is empty")
            return

        message = self._build_startup_message()
        for chat_id in targets:
            try:
                self._send_telegram_system(chat_id, message)
            except Exception as exc:
                self._log(f"startup_telegram_message failed for chat_id={chat_id}: {exc}")

    def _send_shutdown_telegram_message(self) -> None:
        if self.no_telegram:
            return

        targets = sorted(self.allowed_chat_ids)
        if not targets:
            self._log("shutdown_telegram_message skipped: allowed_chat_ids is empty")
            return

        message = (
            "Bridge stopped.\n"
            f"Profile: {self.profile_id}\n"
            f"Thread: {self.thread_id or '(not started)'}\n"
            f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        for chat_id in targets:
            try:
                self._send_telegram_system(chat_id, message)
            except Exception as exc:
                self._log(f"shutdown_telegram_message failed for chat_id={chat_id}: {exc}")

    def _send_telegram_final(
        self,
        chat_id: int,
        rc: int,
        final_text: str,
        *,
        interrupted: bool = False,
    ) -> None:
        if interrupted:
            prefix = "Interrupted."
        else:
            prefix = "Done." if rc == 0 else f"Done with issues (rc={rc})."
        body, _ = compact_for_telegram(
            final_text,
            max_chars=self.telegram_summary_max_chars,
            max_lines=self.telegram_summary_max_lines,
            force_summary=self.telegram_force_summary,
        )
        plain_payload = f"{prefix}\n\n{body}"

        if self.telegram_format_mode == "plain":
            self._send_telegram(chat_id, plain_payload)
            return

        if self.thread_title:
            html_prefix = f"<b>[{html.escape(self.thread_title)}]</b> <b>{html.escape(prefix)}</b>"
        else:
            html_prefix = f"<b>{html.escape(prefix)}</b>"
        html_body = markdown_to_telegram_html(body)
        payload = f"{html_prefix}\n\n{html_body}"

        # If rendered HTML is too large, fallback to plain chunked text.
        if len(payload) > MAX_TELEGRAM_TEXT:
            self._send_telegram(chat_id, plain_payload)
            return

        try:
            self._send_telegram_raw(chat_id, payload, parse_mode="HTML")
        except Exception:
            # Keep delivery robust even if HTML parse fails on edge content.
            self._send_telegram(chat_id, plain_payload)

    def _fetch_updates(self) -> list[dict[str, Any]]:
        if self.no_telegram:
            return []

        result = self._tg_call(
            "getUpdates",
            {
                "offset": self.telegram_offset,
                "timeout": self.poll_timeout,
                "allowed_updates": ["message"],
            },
            timeout=self.poll_timeout + 5,
        )
        if not isinstance(result, list):
            return []
        return [x for x in result if isinstance(x, dict)]

    # -------------------------
    # Commands/status
    # -------------------------
    def _status_text(self) -> str:
        qsize = self._queue.qsize()
        status = "busy" if self._busy else "idle"
        current = self._current_task.source if self._current_task else "-"
        with self._proc_lock:
            limit_context = self._limit_context_window or "(unknown)"
            limit_5h = self._limit_5h or "(unknown)"
            limit_weekly = self._limit_weekly or "(unknown)"
        return (
            f"Profile: {self.profile_id}\n"
            f"Project: {self.project_path}\n"
            f"Thread: {self.thread_id or '(not started)'}\n"
            f"Thread title: {self.thread_title or '-'}\n"
            f"Status: {status}\n"
            f"Current task source: {current}\n"
            f"Queue size: {qsize}\n"
            "Codex limits usage:\n"
            f"- Context window: {limit_context}\n"
            f"- 5h limit: {limit_5h}\n"
            f"- Weekly limit: {limit_weekly}"
        )

    def _permissions_text(self) -> str:
        sandbox = self.codex_permissions_mode or "(codex default)"
        approval = self.codex_approval_policy or "(codex default)"
        web_search = "enabled" if self.codex_web_search else "disabled"
        return (
            "Codex permissions:\n"
            f"Sandbox: {sandbox}\n"
            f"Approval policy: {approval}\n"
            f"Web search: {web_search}"
        )

    def _interrupt_active_execution(self, *, source: str) -> str:
        with self._proc_lock:
            proc = self._active_proc
            if proc is None or proc.poll() is not None:
                return "No active Codex execution to interrupt."
            self._interrupt_requested = True
            self._interrupt_reason = source

        errors: list[str] = []
        signaled = False

        if os.name == "nt":
            # Codex may spawn child processes on Windows. Kill the full process tree
            # so /esc reliably stops long-running tool calls.
            try:
                completed = subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=5,
                )
                if completed.returncode == 0 or proc.poll() is not None:
                    signaled = True
                else:
                    out = (completed.stdout or "").strip()
                    errors.append(f"taskkill rc={completed.returncode}: {out or 'no output'}")
            except Exception as exc:
                errors.append(f"taskkill failed: {exc}")
        else:
            try:
                proc.terminate()
                signaled = True
            except Exception as exc:
                errors.append(f"terminate failed: {exc}")

        deadline = time.time() + 1.5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)

        if proc.poll() is None:
            try:
                proc.kill()
                signaled = True
            except Exception as exc:
                errors.append(f"kill failed: {exc}")

        if signaled:
            return "Interrupt signal sent to active Codex execution."
        details = "; ".join(errors) if errors else "unknown error"
        return f"Failed to interrupt execution: {details}"

    def _is_execution_active(self) -> bool:
        with self._proc_lock:
            proc = self._active_proc
            return bool(proc is not None and proc.poll() is None)

    def _telegram_commands_for_mode(self) -> tuple[str, list[dict[str, str]]]:
        if self._is_execution_active():
            return (
                "exec",
                [
                    {"command": "esc", "description": "Interrupt active execution"},
                ],
            )
        return (
            "idle",
            [
                {"command": "status", "description": "Bridge status and queue size"},
                {"command": "permissions", "description": "Current Codex permissions"},
                {"command": "thread", "description": "Show current thread id"},
                {"command": "newsession", "description": "Reset thread for next task"},
                {"command": "queue", "description": "Show queue size"},
                {"command": "ping", "description": "Health check"},
                {"command": "help", "description": "Show available commands"},
            ],
        )

    def _help_text(self) -> str:
        if self._is_execution_active():
            return (
                "Commands while Codex is running:\n"
                "/esc - interrupt active Codex execution"
            )

        return (
            "Commands:\n"
            "/help - show this help\n"
            "/status - current status\n"
            "/permissions - current Codex permission settings\n"
            "/thread - show current thread id\n"
            "/newsession - reset thread (next task starts a new thread)\n"
            "/queue - queue size\n"
            "/ping - health check\n"
            "/esc - available only while Codex is running"
        )

    def _sync_telegram_commands(self, *, force: bool = False) -> bool:
        if self.no_telegram:
            return True
        mode, commands = self._telegram_commands_for_mode()
        if not force and mode == self._telegram_commands_mode:
            return True
        scopes = self._telegram_command_apply_scopes()
        all_ok = True
        for scope_payload in scopes:
            payload = {"commands": commands, **scope_payload}
            last_exc: Exception | None = None
            ok = False
            for attempt in range(2):
                try:
                    self._tg_call("setMyCommands", payload, timeout=20)
                    ok = True
                    break
                except Exception as exc:
                    last_exc = exc
                    self._log(f"setMyCommands failed (attempt {attempt + 1}/2): {exc}")
                    if attempt == 0:
                        time.sleep(0.3)
            if not ok:
                all_ok = False
                if last_exc is not None:
                    self._log(f"setMyCommands not applied for scope={scope_payload!r}: {last_exc}")
        if all_ok:
            self._telegram_commands_mode = mode
            return True
        self._telegram_commands_mode = None
        return False

    def _telegram_command_apply_scopes(self) -> list[dict[str, Any]]:
        # In restricted mode, bind commands to explicit chat scopes so Telegram
        # clients don't keep showing stale commands from a different scope.
        if self.allowed_chat_ids:
            scopes: list[dict[str, Any]] = []
            for chat_id in sorted(self.allowed_chat_ids):
                scopes.append({"scope": {"type": "chat", "chat_id": chat_id}})
            return scopes
        return [{}]

    def _telegram_command_clear_scopes(self) -> list[dict[str, Any]]:
        # Clear active scopes plus default legacy scope left by older builds.
        scopes = self._telegram_command_apply_scopes()
        has_default = any(
            isinstance(x.get("scope"), dict) and x.get("scope", {}).get("type") == "default"
            for x in scopes
            if isinstance(x, dict)
        )
        if not has_default:
            scopes = [*scopes, {"scope": {"type": "default"}}]
        return scopes

    def _clear_telegram_commands(self) -> None:
        if self.no_telegram:
            return
        for scope_payload in self._telegram_command_clear_scopes():
            try:
                self._tg_call("deleteMyCommands", scope_payload, timeout=20)
            except Exception as exc:
                self._log(f"deleteMyCommands failed for scope={scope_payload!r}: {exc}")
                try:
                    self._tg_call("setMyCommands", {"commands": [], **scope_payload}, timeout=20)
                except Exception as nested_exc:
                    self._log(f"setMyCommands([]) failed for scope={scope_payload!r}: {nested_exc}")
        self._telegram_commands_mode = None

    # -------------------------
    # Message intake
    # -------------------------
    def _enqueue(self, task: Task) -> int:
        task.enqueued_at = now_ts()
        self._queue.put(task)
        return self._queue.qsize()

    def _handle_telegram_command(self, chat_id: int, text: str) -> None:
        cmd = text.strip().split()[0].lower()
        execution_active = self._is_execution_active()

        if execution_active:
            if cmd == "/esc":
                self._send_telegram_system(chat_id, self._interrupt_active_execution(source="telegram:/esc"))
                return
            self._send_telegram_system(chat_id, "Codex is running. Only /esc is available right now.")
            return

        if cmd == "/esc":
            self._send_telegram_system(chat_id, "/esc is available only while Codex is running.")
            return

        if cmd == "/help":
            self._send_telegram_system(chat_id, self._help_text())
            return
        if cmd == "/status":
            self._refresh_limits_usage()
            self._send_telegram_system(chat_id, self._status_text())
            return
        if cmd == "/permissions":
            self._send_telegram_system(chat_id, self._permissions_text())
            return
        if cmd == "/thread":
            self._send_telegram_system(chat_id, self.thread_id or "(not started)")
            return
        if cmd == "/newsession":
            self.thread_id = None
            self.thread_title_applied_for = None
            self.thread_title_applied_value = None
            self._save_state()
            self._send_telegram_system(chat_id, "Thread reset. Next task will start a new Codex thread.")
            return
        if cmd == "/queue":
            self._send_telegram_system(chat_id, f"Queue size: {self._queue.qsize()}")
            return
        if cmd == "/ping":
            self._send_telegram_system(chat_id, "pong")
            return

        self._send_telegram_system(chat_id, "Unknown command. Use /help.")

    def _handle_local_command(self, text: str) -> bool:
        cmd = text.strip().split()[0].lower()
        if cmd in {"/help", "help"}:
            self._log(self._help_text().replace("\n", " | "))
            self._log("Local-only: /exit stops bridge.")
            return True
        if cmd in {"/status", "status"}:
            self._refresh_limits_usage()
            self._log(self._status_text().replace("\n", " | "))
            return True
        if cmd in {"/permissions", "permissions"}:
            self._log(self._permissions_text().replace("\n", " | "))
            return True
        if cmd in {"/esc", "esc"}:
            self._log(self._interrupt_active_execution(source="local:/esc"))
            return True
        if cmd in {"/thread", "thread"}:
            self._log(f"thread={self.thread_id or '(not started)'}")
            return True
        if cmd in {"/newsession", "newsession"}:
            self.thread_id = None
            self.thread_title_applied_for = None
            self.thread_title_applied_value = None
            self._save_state()
            self._log("Thread reset.")
            return True
        if cmd in {"/queue", "queue"}:
            self._log(f"queue={self._queue.qsize()}")
            return True
        if cmd in {"/exit", "exit", "quit"}:
            self._log("Stopping bridge...")
            self._running = False
            return True
        return False

    # -------------------------
    # Telegram polling thread
    # -------------------------
    def _telegram_loop(self) -> None:
        self._log("Telegram poller started.")
        while self._running:
            try:
                updates = self._fetch_updates()
            except Exception as exc:
                self._log(f"Telegram polling error: {exc}")
                time.sleep(3)
                continue

            if not updates:
                continue

            for upd in updates:
                update_id = upd.get("update_id")
                if isinstance(update_id, int):
                    self.telegram_offset = max(self.telegram_offset, update_id + 1)

                message = upd.get("message")
                if not isinstance(message, dict):
                    continue
                text_raw = message.get("text")
                caption_raw = message.get("caption")
                text = text_raw if isinstance(text_raw, str) else (caption_raw if isinstance(caption_raw, str) else "")

                chat = message.get("chat") or {}
                chat_id = chat.get("id")
                if not isinstance(chat_id, int):
                    continue

                if self.allowed_chat_ids and chat_id not in self.allowed_chat_ids:
                    if chat_id not in self._unauthorized_warned:
                        self._unauthorized_warned.add(chat_id)
                        try:
                            self._send_telegram(chat_id, "Unauthorized chat for this bridge profile.")
                        except Exception:
                            pass
                    continue

                attachments, warnings = self._extract_telegram_attachments(message, chat_id)
                if warnings:
                    self._send_telegram_system(chat_id, "Failed to download some attachments:\n- " + "\n- ".join(warnings))

                clean = text.strip()
                if not clean and not attachments:
                    continue

                if clean.startswith("/") and not attachments:
                    self._handle_telegram_command(chat_id, clean)
                    continue

                prompt = self._build_prompt_from_telegram_message(clean, attachments)
                if not prompt:
                    continue

                qsize = self._enqueue(
                    Task(
                        source="telegram",
                        prompt=prompt,
                        chat_id=chat_id,
                        message_id=message.get("message_id"),
                        image_paths=[str(item["path"]) for item in attachments if bool(item.get("is_image"))],
                    )
                )

                self._log_verbose(f"Telegram task queued from chat_id={chat_id}, queue={qsize}")

            self._save_state()

    # -------------------------
    # Local stdin thread
    # -------------------------
    def _local_input_loop(self) -> None:
        self._log("Local input enabled. Type prompts or /help.")
        while self._running:
            while self._running and (self._busy or not self._queue.empty()):
                time.sleep(0.05)
            if not self._running:
                return

            try:
                line = input("> ").strip()
            except EOFError:
                return
            except KeyboardInterrupt:
                self._running = False
                return

            if not line:
                continue
            if line.startswith("/") and self._handle_local_command(line):
                continue

            qsize = self._enqueue(Task(source="local", prompt=line))
            self._log_verbose(f"Queued local prompt. queue={qsize}")

    # -------------------------
    # Codex execution
    # -------------------------
    def _run_thread_rename(self, thread_id: str, title: str) -> bool:
        cmd = [
            self.codex_bin,
            *self.codex_global_args,
            "exec",
            *self.codex_exec_prefix_args,
            "--color",
            self.codex_color_mode,
            "resume",
            *self.codex_exec_args,
            thread_id,
            f"/rename {title}",
        ]
        self._log_verbose(f"Applying thread title via /rename for thread={thread_id}")
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(self.project_path),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0:
            return True

        tail = (proc.stdout or "").strip()
        if len(tail) > 600:
            tail = tail[-600:]
        self._log(f"Failed to apply /rename for thread={thread_id}, rc={proc.returncode}, output={tail}")
        return False

    def _ensure_thread_title_applied(self) -> None:
        if not self.thread_title:
            return
        if not self.thread_id:
            return
        if (
            self.thread_title_applied_for == self.thread_id
            and self.thread_title_applied_value == self.thread_title
        ):
            return

        if self._run_thread_rename(self.thread_id, self.thread_title):
            self.thread_title_applied_for = self.thread_id
            self.thread_title_applied_value = self.thread_title
            self._save_state()
            self._log(f"Thread title applied: {self.thread_title}")

    def _build_codex_cmd(
        self,
        prompt: str,
        *,
        last_message_file: Path,
        image_paths: list[str] | None = None,
    ) -> list[str]:
        common = [
            self.codex_bin,
            *self.codex_global_args,
            "exec",
            *self.codex_exec_prefix_args,
            "--color",
            self.codex_color_mode,
            "-o",
            str(last_message_file),
        ]
        images = [str(x) for x in (image_paths or []) if str(x).strip()]

        if self.thread_id:
            # Resume the exact stored thread id for deterministic continuity.
            return [
                *common,
                "resume",
                *sum((["--image", p] for p in images), []),
                *self.codex_exec_args,
                self.thread_id,
                prompt,
            ]

        return [
            *common,
            "-C",
            str(self.project_path),
            *sum((["--image", p] for p in images), []),
            *self.codex_exec_args,
            prompt,
        ]

    def _build_codex_limits_probe_cmd(self, *, last_message_file: Path) -> list[str]:
        # Run a lightweight one-off probe to refresh displayed account limits.
        probe_prompt = "Reply with exactly: LIMITS_PROBE_OK. Do not call any tools."
        return [
            self.codex_bin,
            *self.codex_global_args,
            "exec",
            *self.codex_exec_prefix_args,
            "--color",
            self.codex_color_mode,
            "-o",
            str(last_message_file),
            "-C",
            str(self.project_path),
            *self.codex_exec_args,
            probe_prompt,
        ]

    def _refresh_limits_usage(self) -> None:
        if self._is_execution_active():
            return
        if not self._limits_refresh_lock.acquire(blocking=False):
            return
        ts = int(time.time() * 1000)
        last_msg_file = self.state_dir / f"_tmp_limits_probe_{self.profile_id}_{ts}.txt"
        cmd = self._build_codex_limits_probe_cmd(last_message_file=last_msg_file)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=str(self.project_path),
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                creationflags=creationflags,
            )
            out = proc.stdout or ""
            matched = False
            for line in out.splitlines():
                plain_line = ANSI_ESCAPE_RE.sub("", line)
                if self._capture_limits_usage_line(plain_line):
                    matched = True
            if not matched:
                self._log("Limits refresh probe: no limits lines found in output.")
        except subprocess.TimeoutExpired:
            self._log("Limits refresh probe timed out.")
        except Exception as exc:
            self._log(f"Limits refresh probe failed: {exc}")
        finally:
            self._refresh_limits_usage_from_sessions()
            try:
                if last_msg_file.exists():
                    last_msg_file.unlink()
            except Exception:
                pass
            self._limits_refresh_lock.release()

    def _refresh_limits_usage_from_sessions(self) -> None:
        context_line = ""
        five_line = ""
        weekly_line = ""

        payload = self._load_latest_token_count_for_thread()
        if payload:
            info = payload.get("info") if isinstance(payload, dict) else None
            if isinstance(info, dict):
                context_line = self._format_context_limits_line(info)

        rate_limits = None
        if isinstance(payload, dict):
            rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            rate_limits = self._load_latest_global_rate_limits()

        if isinstance(rate_limits, dict):
            primary = rate_limits.get("primary") if isinstance(rate_limits.get("primary"), dict) else None
            secondary = rate_limits.get("secondary") if isinstance(rate_limits.get("secondary"), dict) else None
            if isinstance(primary, dict):
                five_line = self._format_window_limit_line(primary)
            if isinstance(secondary, dict):
                weekly_line = self._format_window_limit_line(secondary)

        with self._proc_lock:
            if context_line:
                self._limit_context_window = context_line
            if five_line:
                self._limit_5h = five_line
            if weekly_line:
                self._limit_weekly = weekly_line

    def _load_latest_token_count_for_thread(self) -> dict[str, Any] | None:
        if not self.thread_id or not self.codex_sessions_dir.exists():
            return None
        pattern = f"*{self.thread_id}*.jsonl"
        candidates = sorted(
            self.codex_sessions_dir.rglob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for session_file in candidates:
            payload = self._load_last_token_count_payload(session_file)
            if payload is not None:
                return payload
        return None

    def _load_latest_global_rate_limits(self) -> dict[str, Any] | None:
        if not self.codex_sessions_dir.exists():
            return None
        candidates = sorted(
            self.codex_sessions_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:40]
        best_ts = ""
        best_rate_limits: dict[str, Any] | None = None
        for session_file in candidates:
            try:
                with session_file.open("r", encoding="utf-8", errors="replace") as fh:
                    for raw in fh:
                        raw = raw.strip()
                        if not raw:
                            continue
                        try:
                            event = json.loads(raw)
                        except Exception:
                            continue
                        if event.get("type") != "event_msg":
                            continue
                        payload = event.get("payload")
                        if not isinstance(payload, dict) or payload.get("type") != "token_count":
                            continue
                        rate_limits = payload.get("rate_limits")
                        if not isinstance(rate_limits, dict):
                            continue
                        ts = str(event.get("timestamp") or "")
                        if ts >= best_ts:
                            best_ts = ts
                            best_rate_limits = rate_limits
            except Exception:
                continue
        return best_rate_limits

    def _load_last_token_count_payload(self, session_file: Path) -> dict[str, Any] | None:
        last_payload: dict[str, Any] | None = None
        try:
            with session_file.open("r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except Exception:
                        continue
                    if event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload")
                    if isinstance(payload, dict) and payload.get("type") == "token_count":
                        last_payload = payload
        except Exception:
            return None
        return last_payload

    def _format_context_limits_line(self, info: dict[str, Any]) -> str:
        model_context_window = info.get("model_context_window")
        if not isinstance(model_context_window, int) or model_context_window <= 0:
            return ""
        used = None
        last_usage = info.get("last_token_usage")
        if isinstance(last_usage, dict):
            used_val = last_usage.get("total_tokens")
            if isinstance(used_val, int) and used_val >= 0:
                used = used_val
        if used is None:
            total_usage = info.get("total_token_usage")
            if isinstance(total_usage, dict):
                used_val = total_usage.get("total_tokens")
                if isinstance(used_val, int) and used_val >= 0:
                    used = min(used_val, model_context_window)
        if used is None:
            return ""
        ratio = min(max(used / float(model_context_window), 0.0), 1.0)
        left_percent = int(round((1.0 - ratio) * 100))
        return f"{left_percent}% left ({self._format_compact_number(used)} used / {self._format_compact_number(model_context_window)})"

    def _format_window_limit_line(self, limit_window: dict[str, Any]) -> str:
        used_percent = limit_window.get("used_percent")
        resets_at = limit_window.get("resets_at")
        if not isinstance(used_percent, (int, float)):
            return ""
        left_percent = int(round(max(0.0, min(100.0, 100.0 - float(used_percent)))))
        filled = int(round((left_percent / 100.0) * 20))
        filled = max(0, min(20, filled))
        bar = "#" * filled + "-" * (20 - filled)
        reset_text = "unknown"
        if isinstance(resets_at, (int, float)) and resets_at > 0:
            try:
                reset_text = datetime.fromtimestamp(float(resets_at)).strftime("%H:%M on %d %b")
            except Exception:
                reset_text = "unknown"
        return f"[{bar}] {left_percent}% left (resets {reset_text})"

    def _format_compact_number(self, value: int) -> str:
        if value >= 1_000_000:
            millions = value / 1_000_000
            return f"{millions:.1f}M".replace(".0M", "M")
        if value >= 1_000:
            thousands = int(round(value / 1_000))
            return f"{thousands}K"
        return str(value)

    def _run_codex_task(
        self,
        prompt: str,
        *,
        image_paths: list[str] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[int, str, bool]:
        self._ensure_thread_title_applied()
        ts = int(time.time() * 1000)
        last_msg_file = self.state_dir / f"_tmp_last_msg_{self.profile_id}_{ts}.txt"
        cmd = self._build_codex_cmd(prompt, last_message_file=last_msg_file, image_paths=image_paths)

        self._log_verbose(f"Running codex (thread={self.thread_id or 'new'})")

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            cwd=str(self.project_path),
            text=False,
            bufsize=0,
            creationflags=creationflags,
        )
        with self._proc_lock:
            self._active_proc = proc
            self._interrupt_requested = False
            self._interrupt_reason = ""
        self._sync_telegram_commands()

        observed_thread_id: str | None = None
        stream_section = ""
        stop_after_exec = False
        interrupted = False
        interrupt_reason = ""
        output_tail: deque[str] = deque(maxlen=20)

        assert proc.stdout is not None
        try:
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break

                # Native passthrough: print codex bytes as-is for CLI-like look/feel.
                sys.stdout.buffer.write(raw)
                sys.stdout.buffer.flush()

                try:
                    line = raw.decode("utf-8", errors="replace")
                except Exception:
                    line = ""

                if line:
                    plain_line = ANSI_ESCAPE_RE.sub("", line)
                    self._capture_limits_usage_line(plain_line)
                    compact = plain_line.strip()
                    if compact:
                        output_tail.append(compact)
                    match = SESSION_ID_RE.search(plain_line)
                    if match:
                        observed_thread_id = match.group(1).lower()

                    if progress_callback is not None:
                        stripped = plain_line.strip()
                        lowered = stripped.lower()

                        if lowered in {"thinking", "user"}:
                            stream_section = lowered
                            stop_after_exec = False
                            continue
                        if lowered in {"assistant", "codex"}:
                            stream_section = lowered
                            stop_after_exec = False
                            continue
                        if lowered == "exec" or lowered.startswith("exec "):
                            stop_after_exec = True
                            stream_section = "exec"
                            continue
                        if lowered.startswith("tokens used"):
                            stream_section = ""
                            stop_after_exec = False
                            continue

                        if stream_section not in {"assistant", "codex"}:
                            continue
                        if stop_after_exec or not stripped:
                            continue
                        if lowered.startswith("mcp"):
                            continue
                        if lowered.startswith("openai codex"):
                            continue
                        if lowered.startswith("workdir:") or lowered.startswith("model:"):
                            continue
                        if lowered.startswith("provider:") or lowered.startswith("approval:"):
                            continue
                        if lowered.startswith("sandbox:") or lowered.startswith("reasoning"):
                            continue
                        if stripped == "--------":
                            continue

                        cleaned = re.sub(r"(?i)^codex[\s:>\-]+", "", stripped).strip()
                        if not cleaned or cleaned.lower() == "codex":
                            continue
                        progress_callback(cleaned)
        finally:
            rc = proc.wait()
            with self._proc_lock:
                interrupted = self._interrupt_requested
                interrupt_reason = self._interrupt_reason
                self._active_proc = None
                self._interrupt_requested = False
                self._interrupt_reason = ""
            self._sync_telegram_commands()

        if observed_thread_id and observed_thread_id != self.thread_id:
            self.thread_id = observed_thread_id
            self._save_state()
            self._log(f"Thread updated: {self.thread_id}")
            self._ensure_thread_title_applied()

        final_text = ""
        if last_msg_file.exists():
            try:
                final_text = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                final_text = ""

        if interrupted:
            suffix = f" ({interrupt_reason})" if interrupt_reason else ""
            final_text = f"Execution interrupted by /esc{suffix}."
        elif not final_text:
            if rc != 0 and output_tail:
                tail = "\n".join(output_tail)
                final_text = (
                    f"(No final agent message captured. exit_code={rc})\n\n"
                    "Last Codex output:\n"
                    f"{tail}"
                )
            else:
                final_text = f"(No final agent message captured. exit_code={rc})"

        try:
            if last_msg_file.exists():
                last_msg_file.unlink()
        except Exception:
            pass

        return rc, final_text, interrupted

    def _capture_limits_usage_line(self, raw_line: str) -> bool:
        clean = raw_line.strip()
        if not clean:
            return False
        clean = clean.strip("│").strip()
        if not clean:
            return False

        context_match = CONTEXT_LIMIT_RE.match(clean)
        if context_match:
            with self._proc_lock:
                self._limit_context_window = context_match.group(1).strip()
            return True

        five_match = FIVE_HOUR_LIMIT_RE.match(clean)
        if five_match:
            with self._proc_lock:
                self._limit_5h = five_match.group(1).strip()
            return True

        weekly_match = WEEKLY_LIMIT_RE.match(clean)
        if weekly_match:
            with self._proc_lock:
                self._limit_weekly = weekly_match.group(1).strip()
            return True
        return False

    # -------------------------
    # Worker loop
    # -------------------------
    def _worker_loop(self) -> None:
        while self._running:
            try:
                task = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue

            self._busy = True
            self._current_task = task
            started = now_ts()

            rc = 1
            final_text = ""
            interrupted = False
            try:
                progress_callback = None
                if task.source == "telegram" and task.chat_id and self.telegram_intermediate_updates:
                    last_sent_at = 0.0
                    last_sent_text = ""

                    def _progress_callback(msg: str) -> None:
                        nonlocal last_sent_at, last_sent_text
                        if not msg or msg == last_sent_text:
                            return
                        now = now_ts()
                        if now - last_sent_at < self.telegram_progress_min_interval_seconds:
                            return
                        try:
                            self._send_telegram(task.chat_id, msg)
                            last_sent_at = now
                            last_sent_text = msg
                        except Exception as exc:
                            self._log(f"Telegram progress send failed: {exc}")

                    progress_callback = _progress_callback

                rc, final_text, interrupted = self._run_codex_task(
                    task.prompt,
                    image_paths=task.image_paths,
                    progress_callback=progress_callback,
                )
            except Exception as exc:
                final_text = f"Bridge execution error: {exc}"
                self._log(final_text)

            elapsed = round(now_ts() - started, 1)
            self._busy = False
            self._current_task = None

            if interrupted:
                self._log(f"Task interrupted in {elapsed}s")
            elif rc != 0:
                self._log(f"Task failed in {elapsed}s, rc={rc}")
            else:
                self._log_verbose(f"Task done in {elapsed}s, rc={rc}")

            if task.source == "telegram" and task.chat_id:
                try:
                    self._send_telegram_final(task.chat_id, rc, final_text, interrupted=interrupted)
                except Exception as exc:
                    self._log(f"Telegram send failed after run: {exc}")

            # Self-heal menu state after each task (important after /esc and transient API failures).
            self._telegram_commands_mode = None
            self._sync_telegram_commands(force=True)

            self._queue.task_done()
            self._save_state()

    # -------------------------
    # Public run API
    # -------------------------
    def run_forever(self) -> int:
        self._acquire_lock()
        self._running = True

        self._log(f"profile={self.profile_id}")
        self._log(f"project={self.project_path}")
        self._log(f"thread={self.thread_id or '(not started)'}")
        self._log(f"thread_title={self.thread_title or '(disabled)'}")
        self._log(f"codex_permissions={self.codex_permissions_mode or '(default)'}")
        self._log(f"codex_approval_policy={self.codex_approval_policy or '(default)'}")
        self._log("codex_web_search=" + ("enabled" if self.codex_web_search else "disabled"))
        if self.no_telegram:
            self._log("telegram=disabled (--no-telegram)")
        else:
            mode = "restricted" if self.allowed_chat_ids else "open"
            self._log(f"telegram=enabled ({mode})")
            self._log(
                "telegram_intermediate_updates="
                + ("enabled" if self.telegram_intermediate_updates else "disabled (final-only)")
            )
            # Hard resync on startup: if previous run died ungracefully, clear stale
            # command menu first and then publish current idle commands.
            self._clear_telegram_commands()
            self._sync_telegram_commands(force=True)
            self._send_startup_telegram_message()

        tg_thread: threading.Thread | None = None
        local_thread: threading.Thread | None = None

        try:
            if not self.no_telegram:
                tg_thread = threading.Thread(target=self._telegram_loop, name="tg-poller", daemon=True)
                tg_thread.start()

            local_thread = threading.Thread(target=self._local_input_loop, name="local-input", daemon=True)
            local_thread.start()

            self._worker_loop()
            return 0
        finally:
            self._running = False
            self._clear_telegram_commands()
            self._send_shutdown_telegram_message()
            self._save_state()
            self._release_lock()
            self._log("Bridge stopped.")

            if tg_thread is not None and tg_thread.is_alive():
                tg_thread.join(timeout=0.2)
            if local_thread is not None and local_thread.is_alive():
                local_thread.join(timeout=0.2)

    def run_once(self, prompt: str) -> int:
        self._acquire_lock()
        try:
            self._log(f"run-once profile={self.profile_id}")
            self._log(f"codex_permissions={self.codex_permissions_mode or '(default)'}")
            self._log(f"codex_approval_policy={self.codex_approval_policy or '(default)'}")
            self._log("codex_web_search=" + ("enabled" if self.codex_web_search else "disabled"))
            rc, _, _ = self._run_codex_task(prompt)
            return rc
        finally:
            self._save_state()
            self._release_lock()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Telegram bridge (native CLI terminal mode)")
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parent),
        help="Bridge root directory (contains profiles/ and state/).",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="Profile name from profiles/<name>.json",
    )
    parser.add_argument(
        "--no-telegram",
        action="store_true",
        help="Disable Telegram polling/sending (local terminal mode only).",
    )
    parser.add_argument(
        "--once",
        default="",
        help="Run one prompt and exit (uses persistent thread if exists).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose bridge logs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        bridge = Bridge(
            root_dir=Path(args.root).resolve(),
            profile_name=args.profile,
            no_telegram=bool(args.no_telegram),
            verbose=bool(args.verbose),
        )
        if args.once:
            return bridge.run_once(args.once)
        return bridge.run_forever()
    except BridgeError as exc:
        print(f"[bridge] ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[bridge] Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
