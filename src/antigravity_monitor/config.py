"""Configuration and filtering/sorting logic for Antigravity Session Monitor.

Implements the specification defined in docs/conversation_list_settings_design.md:
- Global settings schema and defaults
- Local JSON configuration persistence (Dual Persistence)
- Session filtering rules (DONE retention, inactivity threshold, state protection)
- Multi-criteria session sorting
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.antigravity_monitor.session_detector import (
    SessionInfo,
    SessionStatus,
    _parse_db_datetime,
)

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/conversation_settings.json")

DEFAULT_SORT_CRITERIA: list[dict[str, Any]] = [
    {"field": "status", "ascending": False},
    {"field": "updated_at", "ascending": False},
    {"field": "title", "ascending": True},
]


@dataclass
class ConversationListConfig:
    """Configuration model for conversation list display and filtering."""

    sort_criteria: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"field": "status", "ascending": False},
            {"field": "updated_at", "ascending": False},
            {"field": "title", "ascending": True},
        ]
    )
    done_retention_minutes: int = 60
    max_inactivity_days: int = 7
    protect_waiting_sessions: bool = True
    protect_working_sessions: bool = True
    exclude_inactive_unlocked: bool = False
    max_tracked_sessions: int = 20

    def to_dict(self) -> dict[str, Any]:
        """Convert config to serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ConversationListConfig:
        """Create config from dictionary with safety validation and fallback."""
        if not data or not isinstance(data, dict):
            return cls()

        # Validate sort_criteria
        raw_criteria = data.get("sort_criteria")
        sort_criteria = []
        if isinstance(raw_criteria, list):
            valid_fields = {"status", "updated_at", "title"}
            for item in raw_criteria:
                if isinstance(item, dict) and item.get("field") in valid_fields:
                    sort_criteria.append(
                        {
                            "field": item["field"],
                            "ascending": bool(item.get("ascending", False)),
                        }
                    )
        if not sort_criteria:
            sort_criteria = [
                {"field": "status", "ascending": False},
                {"field": "updated_at", "ascending": False},
                {"field": "title", "ascending": True},
            ]

        # Clamp done_retention_minutes (-1 to 525600)
        try:
            done_retention = int(data.get("done_retention_minutes", 60))
            if done_retention < -1:
                done_retention = -1
            elif done_retention > 525600:
                done_retention = 525600
        except (ValueError, TypeError):
            done_retention = 60

        # Clamp max_inactivity_days (0 to 365)
        try:
            max_inactivity = int(data.get("max_inactivity_days", 7))
            if max_inactivity < 0:
                max_inactivity = 0
            elif max_inactivity > 365:
                max_inactivity = 365
        except (ValueError, TypeError):
            max_inactivity = 7

        # Clamp max_tracked_sessions (5 to 100)
        try:
            max_tracked = int(data.get("max_tracked_sessions", 20))
            if max_tracked < 5:
                max_tracked = 5
            elif max_tracked > 100:
                max_tracked = 100
        except (ValueError, TypeError):
            max_tracked = 20

        return cls(
            sort_criteria=sort_criteria,
            done_retention_minutes=done_retention,
            max_inactivity_days=max_inactivity,
            protect_waiting_sessions=bool(data.get("protect_waiting_sessions", True)),
            protect_working_sessions=bool(data.get("protect_working_sessions", True)),
            exclude_inactive_unlocked=bool(
                data.get("exclude_inactive_unlocked", False)
            ),
            max_tracked_sessions=max_tracked,
        )

    @classmethod
    def load_from_file(cls, path: Path | str | None = None) -> ConversationListConfig:
        """Load configuration from a local JSON file with fallback."""
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.is_file():
            logger.info(
                "Config file not found at %s. Using default configuration.", config_path
            )
            return cls()

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as e:
            logger.warning(
                "Failed to load config from %s: %s. Using defaults.", config_path, e
            )
            return cls()

    def save_to_file(self, path: Path | str | None = None) -> None:
        """Save current configuration to a local JSON file."""
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info("Saved configuration to %s", config_path)
        except Exception as e:
            logger.error("Failed to save config to %s: %s", config_path, e)


def parse_session_datetime(val: str | datetime | None) -> datetime | None:
    """Parse session datetime string to UTC-aware datetime."""
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)

    # First try ISO parse via _parse_db_datetime
    dt = _parse_db_datetime(str(val))
    if dt is not None:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # Try standard fromisoformat
    try:
        dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_session_visible(
    session: SessionInfo,
    config: ConversationListConfig,
    now: datetime | None = None,
) -> bool:
    """Determine whether a session should be displayed based on configuration."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    # 1. Exclude inactive (unlocked) sessions if configured
    if config.exclude_inactive_unlocked and not session.is_active_presence:
        return False

    # 2. Protection rules for active sessions
    if (
        config.protect_waiting_sessions
        and session.status == SessionStatus.WAITING_FOR_APPROVAL
    ):
        return True
    if config.protect_working_sessions and session.status == SessionStatus.WORKING:
        return True

    session_dt = parse_session_datetime(session.updated_at)

    # 3. DONE session retention
    if session.status == SessionStatus.DONE:
        # 0: Immediate drop
        if config.done_retention_minutes == 0:
            return False
        # > 0: Drop if older than specified minutes
        if config.done_retention_minutes > 0 and session_dt:
            age_minutes = (now_dt - session_dt).total_seconds() / 60.0
            if age_minutes > config.done_retention_minutes:
                return False
        # -1: Retain indefinitely (proceeds to inactivity check)

    # 4. Inactivity threshold (max_inactivity_days)
    if config.max_inactivity_days > 0 and session_dt:
        age_days = (now_dt - session_dt).total_seconds() / 86400.0
        if age_days > config.max_inactivity_days:
            return False

    return True


def sort_sessions(
    sessions: list[SessionInfo],
    criteria: list[dict[str, Any]],
) -> list[SessionInfo]:
    """Sort sessions according to multi-level criteria."""

    def status_score(status: SessionStatus) -> int:
        if status == SessionStatus.WAITING_FOR_APPROVAL:
            return 2
        elif status == SessionStatus.WORKING:
            return 1
        return 0

    def make_sort_tuple(s: SessionInfo) -> tuple[Any, ...]:
        keys = []
        for crit in criteria:
            field_name = crit.get("field")
            asc = crit.get("ascending", False)

            if field_name == "status":
                val = status_score(s.status)
                keys.append(val if asc else -val)
            elif field_name == "updated_at":
                dt = parse_session_datetime(s.updated_at)
                ts = dt.timestamp() if dt else 0.0
                keys.append(ts if asc else -ts)
            elif field_name == "title":
                t = (s.title or "").lower()
                # For string descending sort in tuple: invert char ordinals or compare directly
                keys.append(t if asc else [-ord(c) for c in t])
        return tuple(keys)

    return sorted(sessions, key=make_sort_tuple)


def filter_and_sort_sessions(
    sessions: list[SessionInfo],
    config: ConversationListConfig,
    now: datetime | None = None,
) -> list[SessionInfo]:
    """Filter, sort, and slice sessions according to configuration."""
    visible = [s for s in sessions if is_session_visible(s, config, now=now)]
    sorted_sessions = sort_sessions(visible, config.sort_criteria)
    return sorted_sessions[: config.max_tracked_sessions]
