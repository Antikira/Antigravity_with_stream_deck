"""Window focus manager for Windows.

Brings target VS Code or terminal windows associated with Antigravity sessions
to the foreground using native Windows Win32 APIs, even when run from background
processes by querying the default user desktop.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

# Constants from Win32 SDK
SW_RESTORE = 9
SW_SHOW = 5
DESKTOP_ALL_ACCESS = 0x01FF

if sys.platform == "win32":
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    LPARAM = ctypes.c_ssize_t
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, LPARAM)
else:
    user32 = None
    kernel32 = None
    LPARAM = ctypes.c_ssize_t
    WNDENUMPROC = None
    wintypes = None


class WindowFocusManager:
    """Manages window discovery and brings windows to the foreground."""

    @classmethod
    def get_visible_windows(cls) -> list[tuple[int, int, str]]:
        """Return a list of (hwnd, pid, title) for all top-level visible windows in desktop."""
        results: list[tuple[int, int, str]] = []
        if sys.platform != "win32":
            return results

        def enum_window_callback(hwnd: int, lparam: int) -> int:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buff = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buff, length + 1)
                    title = buff.value.strip()
                    if title:
                        pid = wintypes.DWORD()
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                        results.append((hwnd, pid.value, title))
            return 1

        cb = WNDENUMPROC(enum_window_callback)

        # Try EnumWindows first
        user32.EnumWindows(cb, 0)

        # If empty (e.g. running from non-interactive thread or service), query 'default' desktop
        if not results:
            hdesk = user32.OpenDesktopW("default", 0, False, DESKTOP_ALL_ACCESS)
            if hdesk:
                try:
                    user32.EnumDesktopWindows(hdesk, cb, 0)
                finally:
                    user32.CloseDesktop(hdesk)

        return results

    @classmethod
    def find_window_for_workspace(cls, workspace_uri: str) -> int | None:
        """Find window handle corresponding to the given workspace URI."""
        parsed = urlparse(workspace_uri)
        path_str = unquote(parsed.path)
        if path_str.startswith("/") and len(path_str) > 2 and path_str[2] == ":":
            path_str = path_str[1:]
        folder_name = path_str.replace("/", "\\").rstrip("\\").split("\\")[-1]

        if not folder_name:
            return None

        windows = cls.get_visible_windows()
        # First preference: VS Code window matching workspace name
        for hwnd, _pid, title in windows:
            t_lower = title.lower()
            is_vscode = "visual studio code" in t_lower or "code" in t_lower
            if folder_name.lower() in t_lower and is_vscode:
                return hwnd

        # Second preference: any window containing folder name
        for hwnd, _pid, title in windows:
            if folder_name.lower() in title.lower():
                return hwnd

        return None

    @classmethod
    def find_window_for_keyword_list(cls, keywords: list[str]) -> int | None:
        """Search visible windows for matches."""
        windows = cls.get_visible_windows()
        for kw in keywords:
            kw_clean = kw.strip().lower()
            if not kw_clean:
                continue
            for hwnd, _pid, title in windows:
                if kw_clean in title.lower():
                    return hwnd
        return None

    @classmethod
    def bring_to_foreground(cls, hwnd: int) -> bool:
        """Bring the specified window handle to the foreground reliably.

        Uses ShowWindowAsync, thread input attachment, and SetForegroundWindow
        to overcome Windows foreground lock constraints.
        """
        if sys.platform != "win32":
            return False

        if not hwnd or not user32.IsWindow(hwnd):
            logger.warning("Invalid or non-existent window handle: %s", hwnd)
            return False

        # If minimized, restore it
        if user32.IsIconic(hwnd):
            user32.ShowWindowAsync(hwnd, SW_RESTORE)
        else:
            user32.ShowWindowAsync(hwnd, SW_SHOW)

        foreground_hwnd = user32.GetForegroundWindow()
        if foreground_hwnd == hwnd:
            return True

        current_thread_id = kernel32.GetCurrentThreadId()
        foreground_thread_id = user32.GetWindowThreadProcessId(foreground_hwnd, None)
        target_thread_id = user32.GetWindowThreadProcessId(hwnd, None)

        attached_foreground = False
        attached_target = False

        try:
            # Attach input threads if different
            if foreground_thread_id and foreground_thread_id != current_thread_id:
                attached_foreground = bool(
                    user32.AttachThreadInput(foreground_thread_id, current_thread_id, True)
                )

            if target_thread_id and target_thread_id != current_thread_id:
                attached_target = bool(
                    user32.AttachThreadInput(current_thread_id, target_thread_id, True)
                )

            user32.BringWindowToTop(hwnd)
            success = bool(user32.SetForegroundWindow(hwnd))
            user32.SetFocus(hwnd)
            return success
        finally:
            if attached_target:
                user32.AttachThreadInput(current_thread_id, target_thread_id, False)
            if attached_foreground:
                user32.AttachThreadInput(foreground_thread_id, current_thread_id, False)

    @classmethod
    def focus_session(
        cls,
        workspace_uris: list[str] | None = None,
        title: str = "",
        fallback_keywords: list[str] | None = None,
    ) -> bool:
        """Focus the window corresponding to the session."""
        # Try finding by workspace URI
        if workspace_uris:
            for uri in workspace_uris:
                hwnd = cls.find_window_for_workspace(uri)
                if hwnd:
                    return cls.bring_to_foreground(hwnd)

        # Try finding by keywords
        keywords: list[str] = []
        if title:
            keywords.append(title)
        if fallback_keywords:
            keywords.extend(fallback_keywords)
        keywords.extend(["Visual Studio Code", "Code", "Antigravity", "Windows Terminal", "pwsh"])

        hwnd = cls.find_window_for_keyword_list(keywords)
        if hwnd:
            return cls.bring_to_foreground(hwnd)

        return False
