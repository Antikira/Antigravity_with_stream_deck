"""Session detector for Antigravity sessions.

Reads conversation summaries, presence locks, and transcript logs to determine
the real-time state of Antigravity sessions.
"""

from __future__ import annotations

import enum
import json
import os
import re
import sqlite3
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.antigravity_monitor.extensibility_contracts import (
    SessionStatusContext,
    SessionStatusRuleProtocol,
    SessionTitleContext,
    TitleResolverRuleProtocol,
)

_NEGATIVE_STATES = ("DENIED", "REJECTED", "CANCEL", "CANCELLED", "CANCELING", "FAILED", "ERROR")

_WAITING_EXACT = frozenset(
    (
        "WAITING",
        "PENDING",
        "WAITING_FOR_APPROVAL",
        "WAITING_FOR_TOOL_APPROVAL",
        "AWAITING_APPROVAL",
        "NEEDS_APPROVAL",
        "REQUIRES_APPROVAL",
        "APPROVAL_REQUIRED",
        "WAITING_FOR_CONFIRMATION",
        "AWAITING_USER_CONFIRMATION",
        "USER_CONFIRMATION_REQUIRED",
        "CONFIRMATION_REQUIRED",
        "WAITING_FOR_USER",
        "WAITING_FOR_INPUT",
        "WAITING_FOR_USER_INPUT",
        "AWAITING_USER_INPUT",
        "NEEDS_USER_INPUT",
        "INPUT_REQUIRED",
        "PERMISSION",
        "PERMISSION_REQUIRED",
        "WAITING_FOR_PERMISSION",
        "AWAITING_PERMISSION",
        "NEEDS_PERMISSION",
        "REQUIRES_PERMISSION",
        "ASK_PERMISSION",
        "REQUEST_PERMISSION",
        "CONFIRM",
        "CONFIRMATION",
        "CONFIRMING",
        "REQUESTED_CHANGES",
    )
)

_WAITING_PREFIXES = tuple(f"{state}_" for state in _WAITING_EXACT)
_WAITING_SUFFIXES = tuple(f"_{state}" for state in _WAITING_EXACT)
_WAITING_KEYWORDS = ("PERMISSION", "APPROVAL", "CONFIRM")


class SessionStatus(str, enum.Enum):
    """Execution status of an Antigravity session."""

    WORKING = "WORKING"  # Working / In progress (AI thinking, executing tools)
    WAITING_FOR_APPROVAL = "WAITING"  # Waiting for user input / tool approval / question
    DONE = "DONE"  # Done / Idle (turn completed, waiting for new request)


def _clean_title_candidate(text: str) -> str:
    """Clean candidate text by stripping XML-like tags and metadata blocks."""
    if not isinstance(text, str) or not text:
        return ""
    # Remove <ADDITIONAL_METADATA>...</ADDITIONAL_METADATA> blocks
    cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", text, flags=re.DOTALL)
    # Remove XML-like system tags like <USER_REQUEST>, </USER_REQUEST>
    cleaned = re.sub(r"</?[A-Z_]+>", "", cleaned)
    for line in cleaned.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def _extract_workspace_name(workspace_uris: list[str]) -> str:
    """Extract folder name from workspace URIs."""
    if not workspace_uris or not isinstance(workspace_uris, list):
        return ""
    for uri in workspace_uris:
        if not isinstance(uri, str) or not uri.strip():
            continue
        decoded = urllib.parse.unquote(uri.strip())
        if decoded.lower().startswith("file://"):
            decoded = decoded[7:]
        cleaned_path = decoded.rstrip("/\\")
        if not cleaned_path:
            continue
        parts = [p for p in re.split(r"[/\\]", cleaned_path) if p]
        if parts:
            candidate = parts[-1].rstrip(":")
            if candidate:
                return candidate
    return ""


class ExplicitTitleRule:
    """Resolve title from explicit session title."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        cleaned = _clean_title_candidate(context.title)
        return cleaned or None


class PreviewTitleRule:
    """Resolve title from session preview text."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        cleaned = _clean_title_candidate(context.preview)
        return cleaned or None


class WorkspaceUriTitleRule:
    """Resolve title from workspace folder name."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        ws_name = _extract_workspace_name(context.workspace_uris)
        return ws_name or None


class FirstUserPromptTitleRule:
    """Resolve title from first user prompt in transcript."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        cleaned = _clean_title_candidate(context.first_user_prompt)
        return cleaned or None


class ConversationIdFallbackTitleRule:
    """Fallback title derived from conversation ID."""

    def resolve(self, context: SessionTitleContext) -> str | None:
        cid = context.conversation_id.strip()
        if cid:
            return f"CLI-{cid[:6]}"
        return None


def _create_default_title_resolvers() -> list[TitleResolverRuleProtocol]:
    return [
        ExplicitTitleRule(),
        PreviewTitleRule(),
        WorkspaceUriTitleRule(),
        FirstUserPromptTitleRule(),
        ConversationIdFallbackTitleRule(),
    ]


DEFAULT_TITLE_RESOLVERS: tuple[TitleResolverRuleProtocol, ...] = tuple(
    _create_default_title_resolvers()
)


def _resolve_session_title(
    title: str,
    preview: str = "",
    workspace_uris: list[str] | None = None,
    first_user_prompt: str = "",
    conversation_id: str = "",
    resolvers: Sequence[TitleResolverRuleProtocol] | None = None,
) -> str:
    """Resolve session title using multi-tier fallback."""
    context = SessionTitleContext(
        title=title,
        preview=preview,
        workspace_uris=list(workspace_uris) if workspace_uris else [],
        first_user_prompt=first_user_prompt,
        conversation_id=conversation_id,
    )
    active_resolvers = DEFAULT_TITLE_RESOLVERS if resolvers is None else resolvers
    for resolver in active_resolvers:
        resolved = resolver.resolve(context)
        if resolved:
            return resolved
    return "Session"


def _has_pending_permission_blob(perm_blob: bytes | None) -> bool:
    """Return True if perm_blob contains a pending (unapproved) permission request."""
    if perm_blob is None or not isinstance(perm_blob, (bytes, bytearray)) or len(perm_blob) == 0:
        return False
    # Decision byte pattern in Protobuf payload:
    # \x10\x01\x18[\x01-\x05]\* indicates an already resolved permission decision
    if re.search(rb"\x10\x01\x18[\x01-\x05]\*", bytes(perm_blob)):
        return False
    return True


@dataclass
class SessionInfo:
    """Information representing an Antigravity session."""

    conversation_id: str
    title: str
    status: SessionStatus
    preview: str = ""
    step_count: int = 0
    updated_at: str = ""
    workspace_uris: list[str] = field(default_factory=list)
    is_active_presence: bool = False
    parent_conversation_id: str = ""
    raw_status: str = ""
    last_step_type: str = ""
    last_step_status: str = ""
    window_handle: int = 0
    process_id: int = 0
    title_resolvers: Sequence[TitleResolverRuleProtocol] | None = field(
        default=None, repr=False, compare=False
    )
    _title_resolved: bool = field(default=False, repr=False, compare=False)

    @property
    def short_title(self) -> str:
        """Return truncated title suitable for Stream Deck key display (max 14 chars)."""
        if self._title_resolved and self.title:
            resolved = self.title
        else:
            resolved = _resolve_session_title(
                self.title,
                self.preview,
                self.workspace_uris,
                conversation_id=self.conversation_id,
                resolvers=self.title_resolvers,
            )
        if len(resolved) > 12:
            return resolved[:11] + "…"
        return resolved

    @property
    def display_status_label(self) -> str:
        """Return user-friendly label for display."""
        if self.status == SessionStatus.WORKING:
            return "稼働中"
        elif self.status == SessionStatus.WAITING_FOR_APPROVAL:
            return "承認待"
        return "完了"


def _parse_db_datetime(dt_str: str) -> datetime | None:
    """Parse sqlite timestamp e.g. 2026-10-02 22:38:57.0189613+00:00."""
    try:
        clean = dt_str.strip()
        if not clean:
            return None
        # Handle space between date and time
        clean = clean.replace(" ", "T")
        if "." in clean:
            parts = clean.split(".")
            sec = parts[0]
            after = parts[1]
            if "+" in after:
                sub, tz = after.split("+", 1)
                tz = "+" + tz
            elif "-" in after:
                sub, tz = after.split("-", 1)
                tz = "-" + tz
            else:
                sub = after
                tz = "+00:00"
            clean = f"{sec}.{sub[:6]}{tz}"
        return datetime.fromisoformat(clean)
    except Exception:
        return None


def _is_waiting_status(value: Any) -> bool:
    """Return whether a status value explicitly indicates a user action is needed."""
    if not isinstance(value, str):
        return False

    normalized = re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_")
    if not normalized:
        return False

    # Negative states that should NOT be considered waiting
    if any(neg in normalized for neg in _NEGATIVE_STATES):
        return False

    if normalized in _WAITING_EXACT:
        return True

    # General keyword matches in composite status strings
    if any(kw in normalized for kw in _WAITING_KEYWORDS):
        return True

    # Check for prefix or suffix via optimized tuple matching
    if normalized.startswith(_WAITING_PREFIXES) or normalized.endswith(_WAITING_SUFFIXES):
        return True

    return False


def _is_permission_or_waiting_tool(name: str) -> bool:
    """Return whether a tool name indicates it requires permission or user approval/input."""
    if not isinstance(name, str) or not name.strip():
        return False

    clean = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")

    # Explicit tools in Antigravity that prompt for user permission or interaction
    known_interactive_tools = {
        "ask_question",
    }
    if clean in known_interactive_tools:
        return True

    # General keywords indicating permission, confirmation, or approval
    keywords = (
        "permission",
        "approval",
        "approve",
        "confirm",
        "question",
        "elicitation",
        "user_input",
    )
    return any(kw in clean for kw in keywords)


class ExplicitWaitingStatusRule:
    """Detect explicit waiting/approval status strings in DB or transcript."""

    def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
        if _is_waiting_status(context.db_status) or _is_waiting_status(
            context.last_transcript_step.get("status")
        ):
            return SessionStatus.WAITING_FOR_APPROVAL
        return None


class ConversationDbWaitingRule:
    """Detect waiting/approval states in the per-conversation SQLite steps DB."""

    def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
        db_step = context.conversation_db_step
        if not db_step:
            return None
        # Antigravity status 9 indicates waiting for user approval / permission
        if db_step.get("status") == 9:
            return SessionStatus.WAITING_FOR_APPROVAL
        if (
            context.is_running
            and db_step.get("status") == 2
            and db_step.get("has_pending_permissions")
        ):
            return SessionStatus.WAITING_FOR_APPROVAL
        return None


class ToolCallWaitingRule:
    """Detect waiting/approval states from transcript tool_calls entries."""

    def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
        tool_calls = context.last_transcript_step.get("tool_calls", [])
        if not isinstance(tool_calls, list):
            return None
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            name = function.get("name") or call.get("name")

            # If the call explicitly specifies a waiting status
            if _is_waiting_status(call.get("status")):
                return SessionStatus.WAITING_FOR_APPROVAL

            # Check for explicit permission / approval flags
            for perm_key in (
                "permission",
                "requires_approval",
                "needs_permission",
                "waiting_for_approval",
                "requires_permission",
            ):
                if call.get(perm_key) is True:
                    return SessionStatus.WAITING_FOR_APPROVAL

            # If tool requires permission (run_command, ask_question, write_to_file, etc.)
            if name and _is_permission_or_waiting_tool(name):
                if context.is_running or name == "ask_question":
                    return SessionStatus.WAITING_FOR_APPROVAL
        return None


class RunningStatusRule:
    """Classify active running sessions as WORKING."""

    def evaluate(self, context: SessionStatusContext) -> SessionStatus | None:
        if context.is_running:
            return SessionStatus.WORKING
        return None


def _create_default_status_rules() -> list[SessionStatusRuleProtocol]:
    return [
        ExplicitWaitingStatusRule(),
        ConversationDbWaitingRule(),
        ToolCallWaitingRule(),
        RunningStatusRule(),
    ]


class SessionDetector:
    """Detects and monitors Antigravity sessions."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        ignore_preexisting_done: bool = True,
        startup_time: datetime | None = None,
        status_rules: Sequence[SessionStatusRuleProtocol] | None = None,
        title_resolvers: Sequence[TitleResolverRuleProtocol] | None = None,
    ):
        if base_dir is None:
            user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
            self.base_dir = Path(user_profile) / ".gemini" / "antigravity"
        else:
            self.base_dir = Path(base_dir)

        self.db_path = self.base_dir / "conversation_summaries.db"
        self.presence_dir = self.base_dir / "presence"
        self.brain_dir = self.base_dir / "brain"
        self.conversations_dir = self.base_dir / "conversations"
        self.ignore_preexisting_done = ignore_preexisting_done
        self.startup_time = startup_time or datetime.now(timezone.utc)
        # Track session IDs that were already active or updated at or after startup
        self._actively_tracked_ids: set[str] = set()
        self._initial_scan_done = False

        # Performance caches
        self._transcript_cache: dict[str, tuple[float, dict[str, Any]]] = {}

        # Extensible status & title rule pipelines
        self._status_rules: list[SessionStatusRuleProtocol] = (
            list(status_rules) if status_rules is not None else _create_default_status_rules()
        )
        self._title_resolvers: list[TitleResolverRuleProtocol] = (
            list(title_resolvers)
            if title_resolvers is not None
            else _create_default_title_resolvers()
        )
        self._has_custom_title_resolvers: bool = title_resolvers is not None

    def register_status_rule(
        self,
        rule: SessionStatusRuleProtocol,
        prepend: bool = True,
    ) -> None:
        """Register a custom status evaluation rule on this detector instance."""
        if prepend:
            self._status_rules.insert(0, rule)
        else:
            self._status_rules.append(rule)

    def register_title_resolver(
        self,
        resolver: TitleResolverRuleProtocol,
        prepend: bool = True,
    ) -> None:
        """Register a custom title resolver rule on this detector instance."""
        if prepend:
            self._title_resolvers.insert(0, resolver)
        else:
            self._title_resolvers.append(resolver)
        self._has_custom_title_resolvers = True

    def get_presence_locked_ids(self) -> set[str]:
        """Return set of conversation IDs that currently have presence lock files."""
        if not self.presence_dir.is_dir():
            return set()
        locked = set()
        for f in self.presence_dir.glob("*.lock"):
            locked.add(f.stem)
        return locked

    def inspect_conversation_db(self, conversation_id: str) -> dict[str, Any]:
        """Inspect conversations/<cid>.db for recent step status and permissions if available."""
        if not conversation_id:
            return {}
        cid_db = self.conversations_dir / f"{conversation_id}.db"
        if not cid_db.is_file():
            return {}

        # DO NOT USE mtime FOR SQLITE FILES in WAL mode, as the `-wal` file can be updated
        # without changing the main file's mtime. We just skip caching here.

        try:
            uri = f"file:{cid_db.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=0.5)
            cur = con.cursor()
            row = cur.execute(
                "SELECT step_type, status, permissions FROM steps ORDER BY idx DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row:
                perm_blob = row[2]
                return {
                    "step_type": row[0],
                    "status": row[1],
                    "has_permissions": perm_blob is not None,
                    "has_pending_permissions": _has_pending_permission_blob(perm_blob),
                }
        except Exception:
            pass

        return {}

    def inspect_transcript(self, conversation_id: str) -> dict[str, Any]:
        """Read the last step from transcript.jsonl if available."""
        transcript_path = (
            self.brain_dir / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        )
        if not transcript_path.is_file():
            return {}

        try:
            mtime = transcript_path.stat().st_mtime
            cached = self._transcript_cache.get(conversation_id)
            if cached and cached[0] == mtime:
                return cached[1]
        except FileNotFoundError:
            return {}

        try:
            # Read last lines efficiently with adequate buffer for tool calls / thinking
            with transcript_path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                chunk_size = min(size, 65536)
                f.seek(max(0, size - chunk_size))
                tail = f.read().decode("utf-8", errors="replace")

            lines = [line.strip() for line in tail.splitlines() if line.strip()]
            if not lines:
                return {}

            # Parse from the last line backwards until a valid JSON step is found
            for line in reversed(lines):
                try:
                    result = json.loads(line)
                    self._transcript_cache[conversation_id] = (mtime, result)
                    return result
                except Exception:
                    continue

            self._transcript_cache[conversation_id] = (mtime, {})
            return {}
        except Exception:
            # Note: mtime is guaranteed to be bound if we reach here
            # because the first thing we do in the main try block is stat() and
            # we handle FileNotFoundError there. If the main try block throws
            # another exception, mtime may not be bound, so we check for its existence.
            if "mtime" in locals():
                self._transcript_cache[conversation_id] = (mtime, {})
            return {}

    def extract_first_user_prompt(self, conversation_id: str) -> str:
        """Extract first user prompt from transcript.jsonl if available."""
        if not conversation_id:
            return ""
        transcript_path = (
            self.brain_dir / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        )
        if not transcript_path.is_file():
            return ""
        try:
            with transcript_path.open("rb") as f:
                chunk = f.read(16384).decode("utf-8", errors="replace")
            for line in chunk.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and data.get("type") == "USER_INPUT":
                        content = data.get("content")
                        if isinstance(content, str):
                            cleaned = _clean_title_candidate(content)
                            if cleaned:
                                return cleaned
                except Exception:
                    continue
        except Exception:
            pass
        return ""

    def determine_status(
        self,
        db_status: str,
        not_fully_idle: int | bool,
        last_transcript_step: dict[str, Any],
        conversation_id: str = "",
    ) -> SessionStatus:
        """Determine whether session is WORKING, WAITING_FOR_APPROVAL, or DONE.

        Evaluates registered status rules in order against a SessionStatusContext,
        falling back to SessionStatus.DONE when no rule matches.
        """
        last_step = last_transcript_step if isinstance(last_transcript_step, dict) else {}
        is_running = (
            bool(not_fully_idle)
            or "RUNNING" in (db_status or "").upper()
            or last_step.get("status") == "RUNNING"
        )
        conversation_db_step = (
            self.inspect_conversation_db(conversation_id) if conversation_id else {}
        )
        context = SessionStatusContext(
            db_status=db_status,
            not_fully_idle=not_fully_idle,
            last_transcript_step=last_step,
            conversation_id=conversation_id,
            is_running=is_running,
            conversation_db_step=conversation_db_step,
        )
        for rule in self._status_rules:
            status = rule.evaluate(context)
            if status is not None:
                return status

        return SessionStatus.DONE

    def list_sessions(
        self,
        limit: int = 20,
        only_main_conversations: bool = True,
    ) -> list[SessionInfo]:
        """Retrieve list of Antigravity sessions ordered by last modified time."""
        if not self.db_path.is_file():
            return []

        locked_ids = self.get_presence_locked_ids()
        sessions: list[SessionInfo] = []
        seen_cids: set[str] = set()

        try:
            # Connect in read-only URI mode to avoid locking
            uri = f"file:{self.db_path.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=2.0)
            cur = con.cursor()

            query = (
                "SELECT conversation_id, title, preview, step_count, "
                "last_modified_time, workspace_uris, status, "
                "parent_conversation_id, not_fully_idle "
                "FROM conversation_summaries "
            )
            conditions = []
            if only_main_conversations:
                # Top-level sessions have empty parent_conversation_id
                conditions.append("(parent_conversation_id IS NULL OR parent_conversation_id = '')")

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY last_modified_time DESC LIMIT ?"

            rows = cur.execute(query, (limit,)).fetchall()
            con.close()

            for row in rows:
                cid = row[0] or ""
                title = row[1] or ""
                preview = row[2] or ""
                step_count = row[3] or 0
                updated_at = str(row[4] or "")
                ws_raw = row[5] or "[]"
                raw_status = row[6] or ""
                parent_cid = row[7] or ""
                not_fully_idle = row[8] or 0

                seen_cids.add(cid)

                try:
                    workspace_uris = json.loads(ws_raw) if isinstance(ws_raw, str) else []
                except Exception:
                    workspace_uris = []

                is_active = cid in locked_ids
                transcript_info = self.inspect_transcript(cid)
                status = self.determine_status(
                    raw_status,
                    not_fully_idle,
                    transcript_info,
                    conversation_id=cid,
                )

                # Filter out sessions that were already DONE prior to startup
                dt_mod = _parse_db_datetime(updated_at)
                is_after_startup = dt_mod is not None and dt_mod >= self.startup_time
                is_currently_active = is_active or status != SessionStatus.DONE

                if is_currently_active or is_after_startup:
                    self._actively_tracked_ids.add(cid)

                if self.ignore_preexisting_done and (cid not in self._actively_tracked_ids):
                    continue

                title_resolved = False
                if self._has_custom_title_resolvers or not title.strip():
                    first_user_prompt = ""
                    if not title.strip() and (
                        self._has_custom_title_resolvers
                        or (
                            not _clean_title_candidate(preview)
                            and not _extract_workspace_name(workspace_uris)
                        )
                    ):
                        first_user_prompt = self.extract_first_user_prompt(cid)
                    title = _resolve_session_title(
                        title,
                        preview=preview,
                        workspace_uris=workspace_uris,
                        first_user_prompt=first_user_prompt,
                        conversation_id=cid,
                        resolvers=self._title_resolvers,
                    )
                    title_resolved = True

                session = SessionInfo(
                    conversation_id=cid,
                    title=title,
                    status=status,
                    preview=preview,
                    step_count=step_count,
                    updated_at=updated_at,
                    workspace_uris=workspace_uris,
                    is_active_presence=is_active,
                    parent_conversation_id=parent_cid,
                    raw_status=raw_status,
                    last_step_type=transcript_info.get("type", ""),
                    last_step_status=transcript_info.get("status", ""),
                    title_resolvers=tuple(self._title_resolvers),
                    _title_resolved=title_resolved,
                )
                sessions.append(session)

        except Exception as e:
            # Fallback or error logging
            print(f"[SessionDetector] Error querying sessions: {e}")

        # Clean up unbounded cache for sessions no longer seen in current scan
        self._transcript_cache = {k: v for k, v in self._transcript_cache.items() if k in seen_cids}

        return sessions
