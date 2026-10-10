"""Tests for Hub settings separation and per-key configuration.

Verifies:
1. HubConfig defaults, validation, and per-key settings extraction.
2. Summary Hub done-to-ready timeout transitions.
3. Separation of settings between multiple Hub keys (key A vs key B).
4. Separation of Hub per-key settings from ConversationListConfig global settings.
5. StreamDeckBridge handling of didReceiveSettings vs didReceiveGlobalSettings.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.antigravity_monitor.config import (
    HubConfig,
)
from src.antigravity_monitor.session_detector import SessionInfo, SessionStatus
from src.antigravity_monitor.session_page_manager import SessionPageManager
from src.antigravity_monitor.streamdeck_bridge import StreamDeckBridge


def test_hub_config_defaults_and_validation():
    """Verify HubConfig default timeout and sort criteria parsing."""
    # 1. Default config
    cfg_default = HubConfig()
    assert cfg_default.hub_done_timeout_seconds == 300.0
    assert cfg_default.is_hub_done_timeout_enabled is True
    assert len(cfg_default.hub_sort_criteria) == 3

    # 2. From empty / None settings
    cfg_none = HubConfig.from_settings(None)
    assert cfg_none.hub_done_timeout_seconds == 300.0
    assert cfg_none.is_hub_done_timeout_enabled is True

    # 3. Custom valid timeout
    cfg_custom = HubConfig.from_settings({"hub_done_timeout_seconds": 60})
    assert cfg_custom.hub_done_timeout_seconds == 60.0
    assert cfg_custom.is_hub_done_timeout_enabled is True

    # 4. Zero seconds
    cfg_zero = HubConfig.from_settings({"hub_done_timeout_seconds": 0})
    assert cfg_zero.hub_done_timeout_seconds == 0.0
    assert cfg_zero.is_hub_done_timeout_enabled is True

    # 5. Negative number disables timeout
    cfg_neg = HubConfig.from_settings({"hub_done_timeout_seconds": -1})
    assert cfg_neg.hub_done_timeout_seconds is None
    assert cfg_neg.is_hub_done_timeout_enabled is False

    # 6. Invalid type disables timeout
    cfg_invalid = HubConfig.from_settings({"hub_done_timeout_seconds": "invalid"})
    assert cfg_invalid.hub_done_timeout_seconds is None
    assert cfg_invalid.is_hub_done_timeout_enabled is False


def test_hub_done_timeout_transitions_to_ready():
    """Verify that a DONE session transitions to READY after timeout."""
    now = datetime.now(timezone.utc)
    old_time = (now - timedelta(seconds=400)).isoformat()
    recent_time = (now - timedelta(seconds=50)).isoformat()

    mgr = SessionPageManager()

    # Session completed 400 seconds ago
    old_session = SessionInfo(
        conversation_id="c_old",
        title="Old Session",
        status=SessionStatus.DONE,
        updated_at=old_time,
    )
    mgr._tracked_session_ids = ["c_old"]
    mgr._cached_sessions = {"c_old": old_session}

    # Default timeout is 300s -> 400s is expired -> Hub shows READY (待機中)
    key_data = mgr.build_summary_hub_key_data(settings={"hub_done_timeout_seconds": 300})
    assert key_data.status == "empty"
    assert key_data.subtitle == "READY"
    assert "待機中" in key_data.title

    # Session completed 50 seconds ago -> within 300s -> Hub shows DONE (完了)
    recent_session = SessionInfo(
        conversation_id="c_recent",
        title="Recent Session",
        status=SessionStatus.DONE,
        updated_at=recent_time,
    )
    mgr._tracked_session_ids = ["c_recent"]
    mgr._cached_sessions = {"c_recent": recent_session}

    key_data_recent = mgr.build_summary_hub_key_data(settings={"hub_done_timeout_seconds": 300})
    assert key_data_recent.status == "done"
    assert key_data_recent.subtitle == "DONE"
    assert "完了 (1)" in key_data_recent.title

    # If timeout is disabled (None / -1), old session still displays as DONE
    mgr._tracked_session_ids = ["c_old"]
    mgr._cached_sessions = {"c_old": old_session}
    key_data_disabled = mgr.build_summary_hub_key_data(settings={"hub_done_timeout_seconds": -1})
    assert key_data_disabled.status == "done"
    assert key_data_disabled.subtitle == "DONE"


def test_per_key_hub_settings_independence():
    """Verify that two Hub keys render independently based on their own settings."""
    now = datetime.now(timezone.utc)
    # Session finished 150 seconds ago
    session_time = (now - timedelta(seconds=150)).isoformat()

    session = SessionInfo(
        conversation_id="sess_150s",
        title="Session 150s",
        status=SessionStatus.DONE,
        updated_at=session_time,
    )

    mgr = SessionPageManager()
    mgr._tracked_session_ids = ["sess_150s"]
    mgr._cached_sessions = {"sess_150s": session}

    # Hub A has timeout 100s (elapsed 150s > 100s -> expired -> READY)
    settings_a = {"hub_done_timeout_seconds": 100}
    key_a = mgr.build_summary_hub_key_data(col=0, row=0, settings=settings_a)
    assert key_a.status == "empty"
    assert key_a.subtitle == "READY"

    # Hub B has timeout 300s (elapsed 150s <= 300s -> not expired -> DONE)
    settings_b = {"hub_done_timeout_seconds": 300}
    key_b = mgr.build_summary_hub_key_data(col=0, row=1, settings=settings_b)
    assert key_b.status == "done"
    assert key_b.subtitle == "DONE"


def test_per_key_hub_sort_criteria_independence():
    """Verify that two Hub keys can prioritize different sessions with per-key criteria."""
    sess_a = SessionInfo(
        conversation_id="sess_a",
        title="Alpha Task",
        status=SessionStatus.WORKING,
        updated_at="2026-10-04T10:00:00Z",
    )
    sess_z = SessionInfo(
        conversation_id="sess_z",
        title="Zulu Task",
        status=SessionStatus.WORKING,
        updated_at="2026-10-04T12:00:00Z",
    )

    mgr = SessionPageManager()
    mgr._tracked_session_ids = ["sess_a", "sess_z"]
    mgr._cached_sessions = {"sess_a": sess_a, "sess_z": sess_z}

    # Hub 1 sorts title ascending (Alpha first)
    settings_hub1 = {
        "hub_sort_criteria": [
            {"field": "status", "ascending": False},
            {"field": "title", "ascending": True},
        ]
    }
    primary_1 = mgr.get_primary_session(settings=settings_hub1)
    assert primary_1 is not None
    assert primary_1.conversation_id == "sess_a"

    # Hub 2 sorts updated_at descending (Zulu first)
    settings_hub2 = {
        "hub_sort_criteria": [
            {"field": "status", "ascending": False},
            {"field": "updated_at", "ascending": False},
        ]
    }
    primary_2 = mgr.get_primary_session(settings=settings_hub2)
    assert primary_2 is not None
    assert primary_2.conversation_id == "sess_z"


def test_bridge_separation_of_hub_and_global_settings():
    """Verify that didReceiveSettings and didReceiveGlobalSettings update separately."""

    async def _test():
        bridge = StreamDeckBridge()

        # Register two keys: Hub Key 1 and Session Slot 1
        await bridge.handle_streamdeck_event(
            {
                "event": "willAppear",
                "action": "com.user.antigravity.summary_hub",
                "context": "ctx_hub_1",
                "payload": {
                    "coordinates": {"column": 0, "row": 2},
                    "settings": {"hub_done_timeout_seconds": 300},
                },
            }
        )

        await bridge.handle_streamdeck_event(
            {
                "event": "willAppear",
                "action": "com.user.antigravity.session_slot_1",
                "context": "ctx_slot_1",
                "payload": {
                    "coordinates": {"column": 1, "row": 0},
                    "settings": {},
                },
            }
        )

        assert bridge.active_contexts["ctx_hub_1"]["settings"]["hub_done_timeout_seconds"] == 300

        # 1. Update Hub Key 1 via didReceiveSettings (per-key instance setting)
        await bridge.handle_streamdeck_event(
            {
                "event": "didReceiveSettings",
                "action": "com.user.antigravity.summary_hub",
                "context": "ctx_hub_1",
                "payload": {
                    "settings": {"hub_done_timeout_seconds": 60},
                },
            }
        )

        # ctx_hub_1 setting is updated
        assert bridge.active_contexts["ctx_hub_1"]["settings"]["hub_done_timeout_seconds"] == 60
        # Global config remains untouched
        assert bridge.state_store.get_config().done_retention_minutes == 60

        # 2. Update Global Settings via didReceiveGlobalSettings (for sort/filter column)
        await bridge.handle_streamdeck_event(
            {
                "event": "didReceiveGlobalSettings",
                "payload": {
                    "settings": {
                        "done_retention_minutes": 1440,
                        "max_inactivity_days": 14,
                    },
                },
            }
        )

        # Global config is updated
        assert bridge.state_store.get_config().done_retention_minutes == 1440
        assert bridge.state_store.get_config().max_inactivity_days == 14
        # Hub Key 1's per-key setting is not overwritten by global settings
        assert bridge.active_contexts["ctx_hub_1"]["settings"]["hub_done_timeout_seconds"] == 60

    asyncio.run(_test())
