"""Background data collector for Antigravity Session Monitor.

Executes all disk I/O, SQLite database queries, and external CLI subprocesses
in dedicated worker threads, ensuring the main asyncio/UI event loop remains
completely non-blocking and responsive.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

from src.antigravity_monitor.extensibility_contracts import (
    QuotaSourceProtocol,
    SessionSourceProtocol,
)
from src.antigravity_monitor.quota_reader import QuotaReader
from src.antigravity_monitor.session_detector import SessionDetector
from src.antigravity_monitor.state_store import StateStore

logger = logging.getLogger(__name__)


class DataCollector:
    """Dedicated background worker that polls sessions and quotas and feeds StateStore."""

    def __init__(
        self,
        state_store: StateStore | None = None,
        session_detector: SessionSourceProtocol | None = None,
        quota_reader: QuotaSourceProtocol | None = None,
        session_poll_interval: float = 1.0,
        quota_poll_interval: float = 120.0,
    ) -> None:
        self.state_store = state_store if state_store is not None else StateStore()
        self.detector: SessionSourceProtocol = (
            session_detector if session_detector is not None else SessionDetector()
        )
        self.quota_reader: QuotaSourceProtocol = (
            quota_reader if quota_reader is not None else QuotaReader()
        )
        self.session_poll_interval = session_poll_interval
        self.quota_poll_interval = quota_poll_interval

        self._is_running = False
        self._stop_event = threading.Event()
        self._mode_lock = threading.Lock()
        self._sleep_event = threading.Event()
        self._wake_event = threading.Event()
        if self.state_store.is_sleeping():
            self._sleep_event.set()
        else:
            self._wake_event.set()
        self._session_thread: threading.Thread | None = None
        self._quota_thread: threading.Thread | None = None
        self._quota_request_queue: queue.Queue[bool | None] = queue.Queue()

    @property
    def is_sleeping(self) -> bool:
        """Return whether the collector is currently paused in sleep mode."""
        return self._sleep_event.is_set()

    def _apply_sleeping_locked(self, target: bool) -> None:
        """Apply sleep/tracking transition while _mode_lock is held."""
        if target:
            self._sleep_event.set()
            self._wake_event.clear()
            while True:
                try:
                    self._quota_request_queue.get_nowait()
                except queue.Empty:
                    break
            self._quota_request_queue.put(None)
            logger.info("DataCollector entered sleep mode (background polling paused).")
        else:
            self._sleep_event.clear()
            self._wake_event.set()
            logger.info("DataCollector resumed tracking mode.")

    def set_sleeping(self, sleeping: bool) -> bool:
        """Enable or disable sleep mode and synchronize StateStore."""
        target = bool(sleeping)
        with self._mode_lock:
            self._apply_sleeping_locked(target)
        self.state_store.set_sleeping(self.is_sleeping)
        return target

    def toggle_sleeping(self) -> bool:
        """Toggle between tracking mode and sleep mode, returning the new sleep state."""
        with self._mode_lock:
            target = not self._sleep_event.is_set()
            self._apply_sleeping_locked(target)
        self.state_store.set_sleeping(self.is_sleeping)
        return target

    def start(self) -> None:
        """Start the background collector threads."""
        if self._is_running:
            return
        self._is_running = True
        self._stop_event.clear()

        # 1. Session collection thread
        self._session_thread = threading.Thread(
            target=self._session_loop,
            daemon=True,
            name="SessionCollectorThread",
        )
        self._session_thread.start()

        # 2. Quota collection thread
        self._quota_thread = threading.Thread(
            target=self._quota_loop,
            daemon=True,
            name="QuotaCollectorThread",
        )
        self._quota_thread.start()

        logger.info("DataCollector threads started successfully.")

    def stop(self) -> None:
        """Stop all collector threads."""
        self._is_running = False
        self._stop_event.set()
        self._wake_event.set()
        self._quota_request_queue.put(None)

        if self._session_thread and self._session_thread.is_alive():
            self._session_thread.join(timeout=1.0)
        if self._quota_thread and self._quota_thread.is_alive():
            self._quota_thread.join(timeout=1.0)

        logger.info("DataCollector stopped.")

    def request_quota_refresh(self) -> None:
        """Trigger an immediate background quota refresh (non-blocking)."""
        if self._sleep_event.is_set():
            logger.debug("Quota refresh ignored while in sleep mode.")
            return
        self._quota_request_queue.put(True)

    def _session_loop(self) -> None:
        """Poll session states on a short interval in a background thread."""
        logger.info("Session collector running (interval: %.1fs)", self.session_poll_interval)
        while not self._stop_event.is_set():
            if self._sleep_event.is_set():
                self._wake_event.wait()
                continue

            try:
                sessions = self.detector.list_sessions(limit=30, only_main_conversations=True)
                if not self._sleep_event.is_set():
                    self.state_store.update_sessions(sessions)
            except Exception as e:
                logger.debug("Error in session collector: %s", e)

            self._stop_event.wait(self.session_poll_interval)

    def _quota_loop(self) -> None:
        """Fetch quotas periodically or on demand in a background thread."""
        logger.info("Quota collector thread running (interval: %.1fs)", self.quota_poll_interval)
        last_quota_time = 0.0
        needs_initial_fetch = True

        # Initial fetch on startup unless starting in sleep mode
        if not self._sleep_event.is_set() and not self._stop_event.is_set():
            needs_initial_fetch = False
            try:
                quotas = self.quota_reader.get_quota_stats(force=True)
                if quotas and not self._sleep_event.is_set():
                    self.state_store.update_quotas(quotas)
                last_quota_time = time.time()
            except Exception as e:
                logger.debug("Error in initial quota fetch: %s", e)
                last_quota_time = time.time()

        while not self._stop_event.is_set():
            if self._sleep_event.is_set():
                self._wake_event.wait()
                continue

            elapsed = time.time() - last_quota_time
            if needs_initial_fetch or elapsed >= self.quota_poll_interval:
                forced = needs_initial_fetch
                needs_initial_fetch = False
            else:
                timeout = max(0.5, self.quota_poll_interval - elapsed)
                try:
                    # Wait for explicit quota request, sleep signal (None), or timer timeout
                    req = self._quota_request_queue.get(timeout=timeout)
                    if self._stop_event.is_set():
                        break
                    if req is None or self._sleep_event.is_set():
                        continue
                    forced = bool(req)
                except queue.Empty:
                    forced = False

            if self._stop_event.is_set() or self._sleep_event.is_set():
                continue

            try:
                quotas = self.quota_reader.get_quota_stats(force=forced)
                if quotas and not self._sleep_event.is_set():
                    self.state_store.update_quotas(quotas)
                last_quota_time = time.time()
            except Exception as e:
                logger.debug("Error updating quota stats: %s", e)
                last_quota_time = time.time()
