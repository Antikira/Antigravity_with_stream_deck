"""Thread-safe state store for Antigravity Session Monitor.

Decouples data collection (background threads) from UI rendering and
WebSocket event handling (main asyncio event loop).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from src.antigravity_monitor.quota_reader import QuotaInfo
from src.antigravity_monitor.session_detector import SessionInfo


@dataclass
class MonitorSnapshot:
    """Immutable snapshot of the monitor state at a point in time."""

    sessions: list[SessionInfo] = field(default_factory=list)
    session_map: dict[str, SessionInfo] = field(default_factory=dict)
    tracked_session_ids: list[str] = field(default_factory=list)
    quotas: dict[str, QuotaInfo] = field(default_factory=dict)
    version: int = 0
    updated_at: float = field(default_factory=time.time)


class StateStore:
    """Thread-safe centralized state store with atomic updates and listener support."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracked_session_ids: list[str] = []
        self._cached_sessions: dict[str, SessionInfo] = {}
        self._cached_quotas: dict[str, QuotaInfo] = {}
        self._version: int = 0
        self._last_updated: float = 0.0
        self._listeners: list[Callable[[MonitorSnapshot], None]] = []

    def add_listener(self, callback: Callable[[MonitorSnapshot], None]) -> None:
        """Register a callback invoked whenever state updates."""
        with self._lock:
            self._listeners.append(callback)

    def get_snapshot(self) -> MonitorSnapshot:
        """Return an instantaneous snapshot of current state (0ms, non-blocking)."""
        with self._lock:
            # Build list of sessions in tracked order
            ordered_sessions = [
                self._cached_sessions[cid]
                for cid in self._tracked_session_ids
                if cid in self._cached_sessions
            ]
            return MonitorSnapshot(
                sessions=ordered_sessions,
                session_map=dict(self._cached_sessions),
                tracked_session_ids=list(self._tracked_session_ids),
                quotas=dict(self._cached_quotas),
                version=self._version,
                updated_at=self._last_updated,
            )

    def update_sessions(self, fresh_sessions: list[SessionInfo]) -> bool:
        """Update session list atomically and maintain stable ordering.

        Returns True if session state has changed.
        """
        fresh_map = {s.conversation_id: s for s in fresh_sessions}

        with self._lock:
            # Maintain stable ordering
            new_tracked = [cid for cid in self._tracked_session_ids if cid in fresh_map]
            for s in fresh_sessions:
                if s.conversation_id not in new_tracked:
                    new_tracked.append(s.conversation_id)

            # Check if any change occurred
            changed = new_tracked != self._tracked_session_ids or fresh_map != self._cached_sessions

            if changed:
                self._tracked_session_ids = new_tracked
                self._cached_sessions = fresh_map
                self._version += 1
                self._last_updated = time.time()
                snapshot = self._build_snapshot_locked()
                listeners = list(self._listeners)
            else:
                return False

        # Notify listeners outside the lock
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                pass

        return True

    def update_quotas(self, fresh_quotas: dict[str, QuotaInfo]) -> bool:
        """Update quota data atomically.

        Returns True if quotas have changed.
        """
        if not fresh_quotas:
            return False

        with self._lock:
            if fresh_quotas == self._cached_quotas:
                return False

            self._cached_quotas = dict(fresh_quotas)
            self._version += 1
            self._last_updated = time.time()
            snapshot = self._build_snapshot_locked()
            listeners = list(self._listeners)

        # Notify listeners outside lock
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                pass

        return True

    def _build_snapshot_locked(self) -> MonitorSnapshot:
        """Build snapshot while lock is already held."""
        ordered_sessions = [
            self._cached_sessions[cid]
            for cid in self._tracked_session_ids
            if cid in self._cached_sessions
        ]
        return MonitorSnapshot(
            sessions=ordered_sessions,
            session_map=dict(self._cached_sessions),
            tracked_session_ids=list(self._tracked_session_ids),
            quotas=dict(self._cached_quotas),
            version=self._version,
            updated_at=self._last_updated,
        )
