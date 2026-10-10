"""Quota and rate limit reader for Antigravity.

Calculates token usage and estimated rate limit consumption for:
- 5-hour limit
- Weekly limit (7 days)
based on Antigravity's agy CLI.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


def _format_tokens(num: int) -> str:
    """Format token count in human-readable format (e.g., 950K, 1.2M)."""
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    if num >= 1_000:
        return f"{num / 1_000:.0f}K"
    return str(num)


def _format_duration(seconds: int) -> str:
    """Format seconds into human-readable duration (e.g. 2h15m, 45m)."""
    if seconds <= 0:
        return "即時"
    hrs = seconds // 3600
    mins = (seconds % 3600) // 60
    if hrs > 0:
        return f"{hrs}h{mins}m"
    return f"{mins}m"


def _parse_iso_datetime(dt_str: str) -> datetime | None:
    """Safely parse ISO timestamp with possible high-precision sub-seconds."""
    try:
        clean = dt_str.replace("Z", "+00:00")
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


@dataclass
class QuotaInfo:
    """Represents quota consumption for a specific time window."""

    window_name: str  # "5H", "7D"
    label: str  # "5H", "週間"
    model_group: str  # "Gemini" or "GPT/Claude"
    remaining_percent: float
    used_tokens: int | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    oldest_token_time: datetime | None = None
    reset_in_seconds: int = 0

    @property
    def used_percent(self) -> float:
        return 100.0 - self.remaining_percent

    @property
    def display_percent_str(self) -> str:
        """Formatted percentage string."""
        pct = min(100.0, max(0.0, self.remaining_percent))
        return f"残{pct:.0f}%"

    @property
    def key_title(self) -> str:
        """Formatted title for Stream Deck key."""
        is_gpt = "GPT" in self.model_group or "Claude" in self.model_group
        model_short = "GPT" if is_gpt else "Gemini"
        line1 = f"{model_short} {self.label}"
        line2 = self.display_percent_str

        if self.oldest_token_time:
            local_dt = self.oldest_token_time.astimezone()
            line3 = local_dt.strftime("%m/%d %H:%M")
        else:
            line3 = "N/A"

        return f"{line1}\n{line2}\n{line3}"

    @property
    def status_level(self) -> str:
        """Return warning level ('normal', 'warning', 'critical')."""
        if self.used_percent >= 90.0:
            return "critical"
        elif self.used_percent >= 75.0:
            return "warning"
        return "normal"


class QuotaReader:
    """Reads and calculates quota consumption using agy CLI with caching and async support."""

    DEFAULT_TTL_SECONDS = 120.0

    def __init__(
        self,
        base_dir: Path | str | None = None,
        cache_ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if base_dir is None:
            user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
            self.base_dir = Path(user_profile) / ".gemini" / "antigravity"
        else:
            self.base_dir = Path(base_dir)

        self.brain_dir = self.base_dir / "brain"
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cached_stats: dict[str, QuotaInfo] = {}
        self._last_fetch_time: float = 0.0
        self._is_fetching = False
        self._lock = threading.Lock()

    @property
    def is_cache_valid(self) -> bool:
        """Check if cached quota stats are still valid within TTL."""
        if not self._cached_stats:
            return False
        return (time.time() - self._last_fetch_time) < self.cache_ttl_seconds

    def get_cached_stats(self) -> dict[str, QuotaInfo]:
        """Return cached quota stats immediately without running any subprocess."""
        with self._lock:
            return dict(self._cached_stats)

    def get_quota_stats(self, force: bool = False) -> dict[str, QuotaInfo]:
        """Calculate quota statistics using agy -p /usage with caching support."""
        if not force and self.is_cache_valid:
            return self.get_cached_stats()

        stats = self._fetch_usage_sync()
        if stats:
            with self._lock:
                self._cached_stats = stats
                self._last_fetch_time = time.time()
            return stats

        return self.get_cached_stats()

    def fetch_in_background(
        self,
        callback: Callable[[dict[str, QuotaInfo]], None] | None = None,
        force: bool = False,
    ) -> None:
        """Fetch quota stats in a background thread and invoke callback upon completion."""
        if not force and self.is_cache_valid:
            if callback:
                callback(self.get_cached_stats())
            return

        with self._lock:
            if self._is_fetching:
                return
            self._is_fetching = True

        def _worker() -> None:
            try:
                stats = self._fetch_usage_sync()
                if stats:
                    with self._lock:
                        self._cached_stats = stats
                        self._last_fetch_time = time.time()
                current = self.get_cached_stats()
                if callback:
                    try:
                        callback(current)
                    except Exception as cb_err:
                        logger.debug("Error in quota callback: %s", cb_err)
            finally:
                with self._lock:
                    self._is_fetching = False

        thread = threading.Thread(target=_worker, daemon=True, name="QuotaFetchThread")
        thread.start()

    def _fetch_usage_sync(self) -> dict[str, QuotaInfo]:
        """Run agy -p /usage synchronously and parse output."""
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                if hasattr(subprocess, "STARTUPINFO"):
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
                    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                    kwargs["startupinfo"] = startupinfo

            result = subprocess.run(
                ["agy", "-p", "/usage"],
                capture_output=True,
                text=True,
                check=True,
                timeout=15.0,
                **kwargs,
            )
            output = result.stdout
        except Exception as e:
            logger.debug("Failed running agy -p /usage: %s", e)
            return {}

        now = datetime.now(timezone.utc)
        stats = {}
        for line in output.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 4:
                model_group_raw = parts[0].strip()
                limit_type = parts[1].strip()
                rem_pct_str = parts[2].strip().rstrip("%")
                reset_time_str = parts[3].strip()

                try:
                    remaining_percent = float(rem_pct_str)
                except ValueError:
                    remaining_percent = 0.0

                reset_time = _parse_iso_datetime(reset_time_str)
                reset_in_seconds = 0
                if reset_time:
                    reset_in_seconds = max(0, int((reset_time - now).total_seconds()))

                is_gemini = "Gemini" in model_group_raw

                if is_gemini:
                    model_group = "Gemini"
                    prefix = "gemini"
                else:
                    model_group = "GPT/Claude"
                    prefix = "gpt"

                if "Five Hour" in limit_type:
                    window_name = "5H"
                    label = "5H"
                    key = f"{prefix}_5h"
                elif "Weekly" in limit_type:
                    window_name = "7D"
                    label = "週間"
                    key = f"{prefix}_weekly"
                else:
                    continue

                stats[key] = QuotaInfo(
                    window_name=window_name,
                    label=label,
                    model_group=model_group,
                    remaining_percent=remaining_percent,
                    oldest_token_time=reset_time,
                    reset_in_seconds=reset_in_seconds,
                )
        return stats
