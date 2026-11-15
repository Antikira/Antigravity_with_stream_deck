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

from src.antigravity_monitor.extensibility_contracts import (
    SessionFilterAndSortRegistry,
)
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

DEFAULT_HUB_SORT_CRITERIA: list[dict[str, Any]] = [
    {"field": "status", "ascending": False},
    {"field": "updated_at", "ascending": False},
    {"field": "title", "ascending": True},
]


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


class InactiveUnlockedFilterRule:
    """Exclude unlocked sessions without active presence when configured."""

    def check_visibility(
        self,
        session: SessionInfo,
        config: ConversationListConfig,
        now: datetime,
    ) -> bool | None:
        if config.exclude_inactive_unlocked and not session.is_active_presence:
            return False
        return None


class ProtectedStatusFilterRule:
    """Protect WAITING_FOR_APPROVAL and WORKING sessions from age-based pruning."""

    def check_visibility(
        self,
        session: SessionInfo,
        config: ConversationListConfig,
        now: datetime,
    ) -> bool | None:
        if config.protect_waiting_sessions and session.status == SessionStatus.WAITING_FOR_APPROVAL:
            return True
        if config.protect_working_sessions and session.status == SessionStatus.WORKING:
            return True
        return None


class DoneRetentionFilterRule:
    """Drop DONE sessions exceeding the configured retention minutes."""

    def check_visibility(
        self,
        session: SessionInfo,
        config: ConversationListConfig,
        now: datetime,
    ) -> bool | None:
        if session.status != SessionStatus.DONE:
            return None
        # 0: Immediate drop
        if config.done_retention_minutes == 0:
            return False
        # > 0: Drop if older than specified minutes
        if config.done_retention_minutes > 0:
            session_dt = parse_session_datetime(session.updated_at)
            if session_dt:
                age_minutes = (now - session_dt).total_seconds() / 60.0
                if age_minutes > config.done_retention_minutes:
                    return False
        # -1: Retain indefinitely (proceeds to inactivity check)
        return None


class InactivityDaysFilterRule:
    """Drop sessions inactive longer than max_inactivity_days."""

    def check_visibility(
        self,
        session: SessionInfo,
        config: ConversationListConfig,
        now: datetime,
    ) -> bool | None:
        if config.max_inactivity_days > 0:
            session_dt = parse_session_datetime(session.updated_at)
            if session_dt:
                age_days = (now - session_dt).total_seconds() / 86400.0
                if age_days > config.max_inactivity_days:
                    return False
        return None


def _status_sort_key(session: SessionInfo, ascending: bool) -> int:
    if session.status == SessionStatus.WAITING_FOR_APPROVAL:
        val = 2
    elif session.status == SessionStatus.WORKING:
        val = 1
    else:
        val = 0
    return val if ascending else -val


def _updated_at_sort_key(session: SessionInfo, ascending: bool) -> float:
    dt = parse_session_datetime(session.updated_at)
    ts = dt.timestamp() if dt else 0.0
    return ts if ascending else -ts


def _title_sort_key(session: SessionInfo, ascending: bool) -> str | list[int]:
    t = (session.title or "").lower()
    # For string descending sort in tuple: invert char ordinals
    return t if ascending else [-ord(c) for c in t]


def create_default_filter_sort_registry() -> SessionFilterAndSortRegistry:
    """Create a SessionFilterAndSortRegistry pre-populated with default rules and fields."""
    registry = SessionFilterAndSortRegistry()
    registry.register_filter_rule(InactiveUnlockedFilterRule())
    registry.register_filter_rule(ProtectedStatusFilterRule())
    registry.register_filter_rule(DoneRetentionFilterRule())
    registry.register_filter_rule(InactivityDaysFilterRule())
    registry.register_sort_field("status", _status_sort_key)
    registry.register_sort_field("updated_at", _updated_at_sort_key)
    registry.register_sort_field("title", _title_sort_key)
    return registry


DEFAULT_FILTER_SORT_REGISTRY = create_default_filter_sort_registry()


def _parse_sort_criteria_list(
    raw_criteria: Any,
    default_criteria: list[dict[str, Any]],
    registry: SessionFilterAndSortRegistry | None = None,
) -> list[dict[str, Any]]:
    """Parse and validate sort criteria against registered sort fields."""
    active_registry = registry or DEFAULT_FILTER_SORT_REGISTRY
    valid_fields = active_registry.valid_sort_fields()
    sort_criteria: list[dict[str, Any]] = []
    if isinstance(raw_criteria, list):
        for item in raw_criteria:
            if isinstance(item, dict) and item.get("field") in valid_fields:
                sort_criteria.append(
                    {
                        "field": item["field"],
                        "ascending": bool(item.get("ascending", False)),
                    }
                )
    if not sort_criteria:
        return [dict(c) for c in default_criteria]
    return sort_criteria


@dataclass
class HubConfig:
    """Per-instance configuration for Summary Hub key."""

    hub_done_timeout_seconds: float | None = 300.0
    hub_sort_criteria: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"field": "status", "ascending": False},
            {"field": "updated_at", "ascending": False},
            {"field": "title", "ascending": True},
        ]
    )

    @property
    def is_hub_done_timeout_enabled(self) -> bool:
        """Check whether done-to-ready timeout transition is active."""
        return self.hub_done_timeout_seconds is not None and self.hub_done_timeout_seconds >= 0

    @classmethod
    def from_settings(
        cls,
        settings: dict[str, Any] | None,
        registry: SessionFilterAndSortRegistry | None = None,
    ) -> HubConfig:
        """Create HubConfig from per-key Stream Deck settings dictionary."""
        if not settings or not isinstance(settings, dict):
            return cls()

        timeout: float | None = 300.0
        if "hub_done_timeout_seconds" in settings:
            raw_timeout = settings.get("hub_done_timeout_seconds")
            if raw_timeout is None:
                timeout = None
            else:
                try:
                    val = float(raw_timeout)
                    if val < 0:
                        timeout = None
                    else:
                        timeout = val
                except (ValueError, TypeError):
                    timeout = None

        sort_criteria = _parse_sort_criteria_list(
            settings.get("hub_sort_criteria"),
            DEFAULT_HUB_SORT_CRITERIA,
            registry=registry,
        )

        return cls(
            hub_done_timeout_seconds=timeout,
            hub_sort_criteria=sort_criteria,
        )


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

    @staticmethod
    def _parse_sort_criteria(
        raw_criteria: Any,
        registry: SessionFilterAndSortRegistry | None = None,
    ) -> list[dict[str, Any]]:
        """Parse and validate sort criteria from raw input."""
        return _parse_sort_criteria_list(
            raw_criteria,
            DEFAULT_SORT_CRITERIA,
            registry=registry,
        )

    @staticmethod
    def _parse_clamped_int(value: Any, default: int, min_val: int, max_val: int) -> int:
        """Parse integer and clamp it between min_val and max_val."""
        try:
            parsed = int(value)
            if parsed < min_val:
                return min_val
            elif parsed > max_val:
                return max_val
            return parsed
        except (ValueError, TypeError):
            return default

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | None,
        registry: SessionFilterAndSortRegistry | None = None,
    ) -> ConversationListConfig:
        """Create config from dictionary with safety validation and fallback."""
        if not data or not isinstance(data, dict):
            return cls()

        return cls(
            sort_criteria=cls._parse_sort_criteria(data.get("sort_criteria"), registry=registry),
            done_retention_minutes=cls._parse_clamped_int(
                data.get("done_retention_minutes", 60), 60, -1, 525600
            ),
            max_inactivity_days=cls._parse_clamped_int(
                data.get("max_inactivity_days", 7), 7, 0, 365
            ),
            protect_waiting_sessions=bool(data.get("protect_waiting_sessions", True)),
            protect_working_sessions=bool(data.get("protect_working_sessions", True)),
            exclude_inactive_unlocked=bool(data.get("exclude_inactive_unlocked", False)),
            max_tracked_sessions=cls._parse_clamped_int(
                data.get("max_tracked_sessions", 20), 20, 5, 100
            ),
        )

    @classmethod
    def load_from_file(cls, path: Path | str | None = None) -> ConversationListConfig:
        """Load configuration from a local JSON file with fallback."""
        config_path = Path(path) if path else DEFAULT_CONFIG_PATH
        if not config_path.is_file():
            logger.info("Config file not found at %s. Using default configuration.", config_path)
            return cls()

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return cls.from_dict(data)
        except Exception as e:
            logger.warning("Failed to load config from %s: %s. Using defaults.", config_path, e)
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


def is_session_visible(
    session: SessionInfo,
    config: ConversationListConfig,
    now: datetime | None = None,
    registry: SessionFilterAndSortRegistry | None = None,
) -> bool:
    """Determine whether a session should be displayed based on configuration."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    active_registry = registry or DEFAULT_FILTER_SORT_REGISTRY
    for rule in active_registry.get_filter_rules():
        decision = rule.check_visibility(session, config, now_dt)
        if decision is not None:
            return decision

    return True


def sort_sessions(
    sessions: list[SessionInfo],
    criteria: list[dict[str, Any]],
    registry: SessionFilterAndSortRegistry | None = None,
) -> list[SessionInfo]:
    """Sort sessions according to multi-level criteria."""
    active_registry = registry or DEFAULT_FILTER_SORT_REGISTRY

    def make_sort_tuple(s: SessionInfo) -> tuple[Any, ...]:
        keys = []
        for crit in criteria:
            field_name = crit.get("field")
            if not isinstance(field_name, str):
                continue
            extractor = active_registry.get_sort_extractor(field_name)
            if extractor is not None:
                asc = bool(crit.get("ascending", False))
                keys.append(extractor(s, asc))
        return tuple(keys)

    return sorted(sessions, key=make_sort_tuple)


def filter_and_sort_sessions(
    sessions: list[SessionInfo],
    config: ConversationListConfig,
    now: datetime | None = None,
    registry: SessionFilterAndSortRegistry | None = None,
) -> list[SessionInfo]:
    """Filter, sort, and slice sessions according to configuration."""
    active_registry = registry or DEFAULT_FILTER_SORT_REGISTRY
    visible = [
        s for s in sessions if is_session_visible(s, config, now=now, registry=active_registry)
    ]
    sorted_sessions = sort_sessions(visible, config.sort_criteria, registry=active_registry)
    return sorted_sessions[: config.max_tracked_sessions]
