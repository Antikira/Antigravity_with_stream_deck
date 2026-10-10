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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionStatus(str, enum.Enum):
    """Execution status of an Antigravity session."""

    WORKING = "WORKING"  # Working / In progress (AI thinking, executing tools)
    WAITING_FOR_APPROVAL = "WAITING"  # Waiting for user input / tool approval / question
    DONE = "DONE"  # Done / Idle (turn completed, waiting for new request)


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

    @property
    def short_title(self) -> str:
        """Return truncated title suitable for Stream Deck key display (max 14 chars)."""
        clean = (self.title or "Session").strip()
        if len(clean) > 12:
            return clean[:11] + "…"
        return clean

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
    negative_states = ("DENIED", "REJECTED", "CANCEL", "CANCELLED", "CANCELING", "FAILED", "ERROR")
    if any(neg in normalized for neg in negative_states):
        return False

    waiting_states = (
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
    if any(
        normalized == state
        or normalized.endswith(f"_{state}")
        or normalized.startswith(f"{state}_")
        for state in waiting_states
    ):
        return True

    # General keyword matches in composite status strings
    if "PERMISSION" in normalized or "APPROVAL" in normalized or "CONFIRM" in normalized:
        return True

    return False


def _is_permission_or_waiting_tool(name: str) -> bool:
    """Return whether a tool name indicates it requires permission or user approval/input."""
    if not isinstance(name, str) or not name.strip():
        return False

    clean = re.sub(r"[^a-zA-Z0-9]+", "_", name.lower()).strip("_")

    # Explicit tools in Antigravity that prompt for user permission or interaction
    known_interactive_tools = {
        "run_command",
        "ask_question",
        "write_to_file",
        "replace_file_content",
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


class SessionDetector:
    """Detects and monitors Antigravity sessions."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        ignore_preexisting_done: bool = True,
        startup_time: datetime | None = None,
    ):
        if base_dir is None:
            user_profile = os.environ.get("USERPROFILE", Path("~").expanduser())
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
        try:
            uri = f"file:{cid_db.as_posix()}?mode=ro"
            con = sqlite3.connect(uri, uri=True, timeout=0.5)
            cur = con.cursor()
            row = cur.execute(
                "SELECT step_type, status, permissions FROM steps ORDER BY idx DESC LIMIT 1"
            ).fetchone()
            con.close()
            if row:
                return {
                    "step_type": row[0],
                    "status": row[1],
                    "has_permissions": row[2] is not None,
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
            # Read last lines efficiently with adequate buffer for tool calls / thinking
            with open(transcript_path, "rb") as f:
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
                    return json.loads(line)
                except Exception:
                    continue
            return {}
        except Exception:
            return {}

    def determine_status(
        self,
        db_status: str,
        not_fully_idle: int | bool,
        last_transcript_step: dict[str, Any],
        conversation_id: str = "",
    ) -> SessionStatus:
        """Determine whether session is WORKING, WAITING_FOR_APPROVAL, or DONE.

        Rules:
        1. If db_status or last transcript step indicates waiting/permission -> WAITING_FOR_APPROVAL
        2. If SQLite conversation DB has status 9 (waiting approval) -> WAITING_FOR_APPROVAL
        3. If last transcript step has tool_calls requiring permission or user input
           while active/running -> WAITING_FOR_APPROVAL
        4. If not_fully_idle or db_status is RUNNING -> WORKING
        5. Otherwise -> DONE
        """
        if _is_waiting_status(db_status) or _is_waiting_status(last_transcript_step.get("status")):
            return SessionStatus.WAITING_FOR_APPROVAL

        is_running = (
            bool(not_fully_idle)
            or "RUNNING" in db_status.upper()
            or last_transcript_step.get("status") == "RUNNING"
        )

        # Check SQLite conversation DB for pending approval status (status 9)
        if conversation_id:
            db_step = self.inspect_conversation_db(conversation_id)
            # Antigravity status 9 indicates waiting for user approval / permission
            if db_step.get("status") == 9:
                return SessionStatus.WAITING_FOR_APPROVAL
            if is_running and db_step.get("status") == 2 and db_step.get("has_permissions"):
                return SessionStatus.WAITING_FOR_APPROVAL

        tool_calls = last_transcript_step.get("tool_calls", [])
        if isinstance(tool_calls, list):
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
                    if is_running or name == "ask_question":
                        return SessionStatus.WAITING_FOR_APPROVAL

        if is_running:
            return SessionStatus.WORKING

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
                )
                sessions.append(session)

        except Exception as e:
            # Fallback or error logging
            print(f"[SessionDetector] Error querying sessions: {e}")

        return sessions
