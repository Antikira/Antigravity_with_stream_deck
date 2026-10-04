"""Key layout manager for Antigravity Stream Deck controller.

Provides 12 Stream Deck buttons:
1. Summary Hub (1 button):
   - Warning light showing highest-priority session status.
   - Color: WAITING (Red) > WORKING (Blue) > DONE (Green) > Idle (Dark Gray).
   - Press to focus the primary (highest-priority) session window.
2. Session 1 to Session 5 (5 buttons):
   - Displays session status for the current page.
   - Press to focus the corresponding session window.
3. Pagination (2 buttons: Prev / Next):
   - Switches the active session page for Session 1-5 buttons.
4. Quota Indicators (4 buttons):
   - Gemini 5-Hour Limit
   - Gemini Weekly Limit
   - Claude/GPT 5-Hour Limit
   - Claude/GPT Weekly Limit
   - Indicator only (no action on press).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from src.antigravity_monitor.config import ConversationListConfig, filter_and_sort_sessions
from src.antigravity_monitor.quota_reader import QuotaInfo, QuotaReader
from src.antigravity_monitor.session_detector import (
    SessionDetector,
    SessionInfo,
    SessionStatus,
)
from src.antigravity_monitor.state_store import StateStore


@dataclass
class KeyRenderData:
    """Render state for a single Stream Deck key."""

    key_id: str  # e.g. "summary_hub", "session_slot_0", "quota_gemini_5h"
    column: int
    row: int
    title: str
    subtitle: str = ""
    status: str = "default"  # "working", "waiting", "done", "normal", "warning", etc.
    bg_color: str = "#222222"
    text_color: str = "#FFFFFF"
    payload: dict[str, Any] | None = None


class SessionPageManager:
    """Manages session slots, quotas, and hub state for Stream Deck keys."""

    # Default grid coordinates for 5 session slots
    SLOT_COORDS = [
        (1, 0),  # Slot 0 -> Session 1
        (2, 0),  # Slot 1 -> Session 2
        (3, 0),  # Slot 2 -> Session 3
        (4, 1),  # Slot 3 -> Session 4
        (4, 2),  # Slot 4 -> Session 5
    ]

    # Session status colors (Priority: Waiting > Working > Done)
    STATUS_COLORS = {
        SessionStatus.WAITING_FOR_APPROVAL: "#E53935",  # Red
        SessionStatus.WORKING: "#1E88E5",  # Blue
        SessionStatus.DONE: "#43A047",  # Green
    }

    # Quota usage level colors
    QUOTA_STATUS_COLORS = {
        "normal": "#2E7D32",  # Forest Green
        "warning": "#F57C00",  # Amber / Orange
        "critical": "#C62828",  # Deep Red
    }

    # Definition of the 4 quota buttons: (quota_key, default_col, default_row, fallback_label)
    QUOTA_DEFINITIONS = [
        ("gemini_5h", 0, 1, "Gemini 5H"),
        ("gemini_weekly", 1, 1, "Gemini 週間"),
        ("gpt_5h", 2, 1, "GPT 5H"),
        ("gpt_weekly", 3, 1, "GPT 週間"),
    ]

    def __init__(
        self,
        session_detector: SessionDetector | None = None,
        quota_reader: QuotaReader | None = None,
        state_store: StateStore | None = None,
        config: ConversationListConfig | None = None,
    ) -> None:
        self.state_store = state_store
        self.detector = session_detector or SessionDetector()
        self.quota_reader = quota_reader or QuotaReader()
        default_cfg = state_store.get_config() if state_store else ConversationListConfig()
        self.config = config or default_cfg
        self.current_page = 0
        self._tracked_session_ids: list[str] = []
        self._cached_sessions: dict[str, SessionInfo] = {}
        self._cached_quotas: dict[str, QuotaInfo] = {}

    # ------------------------------------------------------------------
    # Pagination
    # ------------------------------------------------------------------

    @property
    def items_per_page(self) -> int:
        return 5

    @property
    def slot_coords(self) -> list[tuple[int, int]]:
        return self.SLOT_COORDS

    @property
    def total_pages(self) -> int:
        """Calculate total number of pages."""
        count = len(self._tracked_session_ids)
        if count == 0:
            return 1
        return math.ceil(count / self.items_per_page)

    def prev_page(self) -> bool:
        """Go to previous page. Return True if page changed."""
        if self.current_page > 0:
            self.current_page -= 1
            return True
        return False

    def next_page(self) -> bool:
        """Go to next page. Return True if page changed."""
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            return True
        return False

    # ------------------------------------------------------------------
    # Data Refresh
    # ------------------------------------------------------------------

    def refresh(self, include_quota: bool = False, force_quota: bool = False) -> None:
        """Fetch fresh session and quota states from StateStore or detector."""
        if self.state_store is not None:
            snapshot = self.state_store.get_snapshot()
            self._tracked_session_ids = snapshot.tracked_session_ids
            self._cached_sessions = snapshot.session_map
            self._cached_quotas = snapshot.quotas
        else:
            fresh_sessions = self.detector.list_sessions(
                limit=50,
                only_main_conversations=True,
            )
            filtered = filter_and_sort_sessions(fresh_sessions, self.config)
            self._tracked_session_ids = [s.conversation_id for s in filtered]
            self._cached_sessions = {s.conversation_id: s for s in filtered}
            if include_quota or force_quota:
                self._cached_quotas = self.quota_reader.get_quota_stats(force=force_quota)
            else:
                self._cached_quotas = self.quota_reader.get_cached_stats()

        # Adjust current page if out of bounds
        total = self.total_pages
        if self.current_page >= total:
            self.current_page = max(0, total - 1)

    def set_cached_quotas(self, quotas: dict[str, QuotaInfo]) -> None:
        """Set cached quotas directly."""
        self._cached_quotas = quotas

    # ------------------------------------------------------------------
    # Session Accessors
    # ------------------------------------------------------------------

    def get_sessions_for_current_page(self) -> list[SessionInfo | None]:
        """Return 5 session slots for the current page (None if slot is empty)."""
        start = self.current_page * self.items_per_page
        page_ids = self._tracked_session_ids[start : start + self.items_per_page]

        result: list[SessionInfo | None] = []
        for i in range(self.items_per_page):
            if i < len(page_ids):
                result.append(self._cached_sessions.get(page_ids[i]))
            else:
                result.append(None)
        return result

    def get_session_by_slot(self, slot_index: int) -> SessionInfo | None:
        """Get session assigned to current page's slot (0-4)."""
        slots = self.get_sessions_for_current_page()
        if 0 <= slot_index < len(slots):
            return slots[slot_index]
        return None

    def get_primary_session(self) -> SessionInfo | None:
        """Find the highest-priority session across all tracked sessions."""
        all_sessions = [
            self._cached_sessions[cid]
            for cid in self._tracked_session_ids
            if cid in self._cached_sessions
        ]
        if not all_sessions:
            return None

        waiting = [s for s in all_sessions if s.status == SessionStatus.WAITING_FOR_APPROVAL]
        if waiting:
            return waiting[0]

        working = [s for s in all_sessions if s.status == SessionStatus.WORKING]
        if working:
            return working[0]

        return all_sessions[0]

    # ------------------------------------------------------------------
    # Key Builders
    # ------------------------------------------------------------------

    def build_summary_hub_key_data(
        self,
        col: int = 0,
        row: int = 2,
    ) -> KeyRenderData:
        """Build the 1-button Hub warning light key."""
        all_sessions = [
            self._cached_sessions[cid]
            for cid in self._tracked_session_ids
            if cid in self._cached_sessions
        ]

        waiting_count = sum(
            1 for s in all_sessions if s.status == SessionStatus.WAITING_FOR_APPROVAL
        )
        working_count = sum(1 for s in all_sessions if s.status == SessionStatus.WORKING)
        done_count = sum(1 for s in all_sessions if s.status == SessionStatus.DONE)

        primary = self.get_primary_session()

        if waiting_count > 0:
            status = "waiting"
            bg_color = self.STATUS_COLORS[SessionStatus.WAITING_FOR_APPROVAL]
            short = primary.short_title if primary else ""
            title = f"承認待ち ({waiting_count})\n{short}"
            subtitle = "WAITING"
        elif working_count > 0:
            status = "working"
            bg_color = self.STATUS_COLORS[SessionStatus.WORKING]
            short = primary.short_title if primary else ""
            title = f"作業中 ({working_count})\n{short}"
            subtitle = "WORKING"
        elif done_count > 0:
            status = "done"
            bg_color = self.STATUS_COLORS[SessionStatus.DONE]
            short = primary.short_title if primary else ""
            title = f"完了 ({done_count})\n{short}"
            subtitle = "DONE"
        else:
            status = "empty"
            bg_color = "#263238"
            title = "Antigravity\n(待機中)"
            subtitle = "READY"

        payload: dict[str, Any] = {"action": "activate_primary"}
        if primary:
            payload["conversation_id"] = primary.conversation_id
            payload["workspace_uris"] = primary.workspace_uris
            payload["title"] = primary.title

        return KeyRenderData(
            key_id="summary_hub",
            column=col,
            row=row,
            title=title,
            subtitle=subtitle,
            status=status,
            bg_color=bg_color,
            text_color="#FFFFFF",
            payload=payload,
        )

    def build_session_slot_key_data(
        self,
        slot_idx: int,
        col: int = 0,
        row: int = 0,
    ) -> KeyRenderData:
        """Build render data for a single session slot (slot_idx: 0..4)."""
        session = self.get_session_by_slot(slot_idx)
        key_id = f"session_slot_{slot_idx}"

        if session is None:
            return KeyRenderData(
                key_id=key_id,
                column=col,
                row=row,
                title=f"Session {slot_idx + 1}\n(空)",
                status="empty",
                bg_color="#1a1a1a",
                text_color="#666666",
                payload={"slot": slot_idx, "conversation_id": ""},
            )

        bg = self.STATUS_COLORS.get(session.status, "#333333")
        status_label = session.display_status_label
        title = f"{session.short_title}\n[{status_label}]"

        return KeyRenderData(
            key_id=key_id,
            column=col,
            row=row,
            title=title,
            subtitle=session.status.value,
            status=session.status.value.lower(),
            bg_color=bg,
            text_color="#FFFFFF",
            payload={
                "slot": slot_idx,
                "conversation_id": session.conversation_id,
                "workspace_uris": session.workspace_uris,
                "title": session.title,
            },
        )

    def build_prev_page_key_data(
        self,
        col: int = 0,
        row: int = 0,
    ) -> KeyRenderData:
        """Build render data for previous page button."""
        total = self.total_pages
        cur = self.current_page + 1
        has_prev = self.current_page > 0
        return KeyRenderData(
            key_id="page_prev",
            column=col,
            row=row,
            title=f"◀ 前\n({cur}/{total})",
            status="normal" if has_prev else "disabled",
            bg_color="#37474F" if has_prev else "#212121",
            payload={"action": "prev_page"},
        )

    def build_next_page_key_data(
        self,
        col: int = 4,
        row: int = 0,
    ) -> KeyRenderData:
        """Build render data for next page button."""
        total = self.total_pages
        cur = self.current_page + 1
        has_next = self.current_page < total - 1
        return KeyRenderData(
            key_id="page_next",
            column=col,
            row=row,
            title=f"次 ▶\n({cur}/{total})",
            status="normal" if has_next else "disabled",
            bg_color="#37474F" if has_next else "#212121",
            payload={"action": "next_page"},
        )

    def build_quota_key_data(
        self,
        qkey: str,
        col: int = 0,
        row: int = 0,
        fallback_label: str = "",
    ) -> KeyRenderData:
        """Build render data for a quota button."""
        q_info = self._cached_quotas.get(qkey)
        if q_info:
            bg = self.QUOTA_STATUS_COLORS.get(q_info.status_level, "#333333")
            title = q_info.key_title
            status = q_info.status_level
        else:
            bg = "#333333"
            lbl = fallback_label or qkey
            title = f"{lbl}\n--%"
            status = "normal"

        return KeyRenderData(
            key_id=f"quota_{qkey}",
            column=col,
            row=row,
            title=title,
            status=status,
            bg_color=bg,
            payload={"quota_type": qkey},
        )

    def get_key_data_by_action_id(
        self,
        action_id: str,
        col: int = 0,
        row: int = 0,
        settings: dict[str, Any] | None = None,
    ) -> KeyRenderData | None:
        """Get KeyRenderData directly by action identifier, suffix, or grid coordinates."""
        suffix = action_id.split(".")[-1]

        # 1. Hub
        if suffix == "summary_hub":
            return self.build_summary_hub_key_data(col=col, row=row)

        # 2. Pagination
        if suffix in ("prev_page", "page_prev", "prev_page_corner"):
            return self.build_prev_page_key_data(col=col, row=row)
        if suffix in ("next_page", "page_next", "next_page_corner"):
            return self.build_next_page_key_data(col=col, row=row)

        # 3. Session Slots (Specific slot UUID: session_slot_1..5 or session_slot_0..4)
        if suffix.startswith("session_slot_"):
            slot_part = suffix.replace("session_slot_", "")
            if slot_part.isdigit():
                num = int(slot_part)
                # If 1-based (from UUID session_slot_1..5), map to 0..4
                slot_idx = num - 1 if num in (1, 2, 3, 4, 5) else num
                if 0 <= slot_idx < self.items_per_page:
                    return self.build_session_slot_key_data(slot_idx, col=col, row=row)

        # 4. Generic session slot: com.user.antigravity.session_slot
        if suffix == "session_slot":
            # Determine slot by settings or column index
            if settings and "slot" in settings:
                try:
                    slot_idx = int(settings["slot"])
                    if 0 <= slot_idx < self.items_per_page:
                        return self.build_session_slot_key_data(slot_idx, col=col, row=row)
                except Exception:
                    pass
            # Fallback: map by grid column (0..4)
            slot_idx = min(max(0, col), self.items_per_page - 1)
            return self.build_session_slot_key_data(slot_idx, col=col, row=row)

        # 5. Quota keys by explicit key name
        for qkey, _, _, fallback in self.QUOTA_DEFINITIONS:
            if suffix in (f"quota_{qkey}", qkey):
                return self.build_quota_key_data(qkey, col=col, row=row, fallback_label=fallback)

        # 6. Legacy / Generic Quota keys (e.g. quota_5h, quota_daily, quota_weekly, or quota)
        if "quota" in suffix:
            quota_keys = ["gemini_5h", "gemini_weekly", "gpt_5h", "gpt_weekly"]
            # Map by column if placed side-by-side (col 1..4 or 0..3)
            q_idx = 0
            if col in (1, 2, 3, 4):
                q_idx = col - 1
            elif col in (0, 1, 2, 3):
                q_idx = col
            target_qkey = quota_keys[min(q_idx, len(quota_keys) - 1)]
            return self.build_quota_key_data(target_qkey, col=col, row=row)

        return None

    def build_all_key_data(self) -> list[KeyRenderData]:
        """Generate render data for all 12 default buttons."""
        keys: list[KeyRenderData] = []

        # 1. Hub
        keys.append(self.build_summary_hub_key_data(col=0, row=2))

        # 2. Pagination
        keys.append(self.build_prev_page_key_data(col=0, row=0))
        keys.append(self.build_next_page_key_data(col=4, row=0))

        # 3. Quota Keys (Row 1: col 0..3)
        for qkey, col, row, fallback in self.QUOTA_DEFINITIONS:
            keys.append(self.build_quota_key_data(qkey, col=col, row=row, fallback_label=fallback))

        # 4. Session Slots 1..5
        for slot_idx, (col, row) in enumerate(self.SLOT_COORDS):
            keys.append(self.build_session_slot_key_data(slot_idx, col=col, row=row))

        return keys
