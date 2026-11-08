"""Thin, dependency-light Win32 layer: windows, input, clipboard, processes.

Only ``ctypes`` and the standard library are used, so this module imports on a
machine where nothing was pip-installed.  Everything is best-effort and returns
plain data; the callers (``uia``, ``apps``, ``agent``) add the policy.

The one non-obvious part is :func:`focus` - ``SetForegroundWindow`` is refused
when the calling process does not own the foreground, which is the normal case
for an MCP server spawned by an editor.  The ``AttachThreadInput`` dance below is
the standard way around that restriction and is what every automation tool does.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "WindowInfo",
    "list_windows",
    "window_for_pid",
    "find_windows",
    "focus",
    "show",
    "is_foreground",
    "window_rect",
    "send_keys",
    "type_text",
    "press",
    "hotkey",
    "click_at",
    "move_to",
    "scroll",
    "clipboard_get",
    "clipboard_set",
    "pid_of_image",
    "pids_of_images",
    "image_path",
    "is_64bit",
    "dpi_aware",
    "VK",
]

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except OSError:  # pragma: no cover - pre-Vista
        dwmapi = None
else:  # pragma: no cover - the whole package is Windows-first
    user32 = kernel32 = dwmapi = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# virtual keys
# --------------------------------------------------------------------------- #
class VK:
    """The subset of virtual-key codes this package uses by name."""

    BACK, TAB, RETURN, ESCAPE, SPACE = 0x08, 0x09, 0x0D, 0x1B, 0x20
    PRIOR, NEXT, END, HOME = 0x21, 0x22, 0x23, 0x24
    LEFT, UP, RIGHT, DOWN = 0x25, 0x26, 0x27, 0x28
    INSERT, DELETE = 0x2D, 0x2E
    SHIFT, CONTROL, MENU = 0x10, 0x11, 0x12
    LSHIFT, RSHIFT, LCONTROL, RCONTROL, LMENU, RMENU = 0x10, 0xA0, 0xA2, 0xA3, 0xA4, 0xA5
    LWIN, RWIN = 0x5B, 0x5C
    F1, F12 = 0x70, 0x7B
    NUMPAD0, NUMPAD9 = 0x60, 0x69
    OEM_PLUS, OEM_MINUS, OEM_COMMA, OEM_PERIOD = 0xBB, 0xBD, 0xBC, 0xBE


#: Names accepted by :func:`press` / :func:`hotkey`, plus single characters.
_KEY_NAMES: dict[str, int] = {
    "backspace": VK.BACK, "back": VK.BACK, "tab": VK.TAB, "enter": VK.RETURN,
    "return": VK.RETURN, "esc": VK.ESCAPE, "escape": VK.ESCAPE, "space": VK.SPACE,
    "pageup": VK.PRIOR, "pagedown": VK.NEXT, "end": VK.END, "home": VK.HOME,
    "left": VK.LEFT, "up": VK.UP, "right": VK.RIGHT, "down": VK.DOWN,
    "insert": VK.INSERT, "delete": VK.DELETE, "del": VK.DELETE,
    "shift": VK.SHIFT, "ctrl": VK.CONTROL, "control": VK.CONTROL, "alt": VK.MENU,
    "win": VK.LWIN, "meta": VK.LWIN,
    "plus": VK.OEM_PLUS, "minus": VK.OEM_MINUS, "comma": VK.OEM_COMMA,
    "period": VK.OEM_PERIOD,
}
for _i in range(1, 13):
    _KEY_NAMES[f"f{_i}"] = 0x70 + _i - 1
for _i in range(10):
    _KEY_NAMES[f"numpad{_i}"] = 0x60 + _i


def key_to_vk(name: str) -> int:
    """``"delete"``/``"F5"``/``"a"`` -> a virtual-key code.  Raises ``KeyError``."""
    lowered = str(name).strip().lower()
    if lowered in _KEY_NAMES:
        return _KEY_NAMES[lowered]
    if len(lowered) == 1:
        char = lowered.upper()
        if char.isalnum():
            return ord(char)
        if char == " ":
            return VK.SPACE
    vk = getattr(VK, lowered.upper(), None)
    if isinstance(vk, int):
        return vk
    raise KeyError(f"unknown key {name!r}")


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    class_name: str
    rect: tuple[int, int, int, int]
    visible: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "pid": self.pid,
            "title": self.title,
            "class": self.class_name,
            "rect": list(self.rect),
            "visible": self.visible,
        }


def dpi_aware() -> None:
    """Make this process DPI-aware so ``GetWindowRect`` matches UIA coordinates."""
    if not IS_WINDOWS:
        return
    try:
        # -4 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


_EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    if not IS_WINDOWS:
        return (0, 0, 0, 0)
    rect = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def _info(hwnd: int) -> WindowInfo | None:
    if not hwnd or not user32.IsWindow(hwnd):
        return None
    title = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, 512)
    klass = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, klass, 256)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowInfo(
        hwnd=int(hwnd),
        pid=int(pid.value),
        title=title.value,
        class_name=klass.value,
        rect=window_rect(hwnd),
        visible=bool(user32.IsWindowVisible(hwnd)),
    )


def list_windows(visible_only: bool = True) -> list[WindowInfo]:
    """Every top-level window, optionally only the visible ones."""
    if not IS_WINDOWS:
        return []
    out: list[WindowInfo] = []

    def callback(hwnd: int, _lparam: int) -> bool:
        info = _info(hwnd)
        if info is not None and (info.visible or not visible_only) and (info.title or info.class_name):
            out.append(info)
        return True

    user32.EnumWindows(_EnumProc(callback), 0)
    return out


def find_windows(title: str = "", class_name: str = "", pid: int = 0) -> list[WindowInfo]:
    """Substring/case-insensitive match on title, exact on class, or by pid."""
    needle = title.lower()
    found = []
    for info in list_windows(visible_only=False):
        if pid and info.pid != pid:
            continue
        if class_name and info.class_name != class_name:
            continue
        if needle and needle not in info.title.lower():
            continue
        found.append(info)
    return found


def window_for_pid(pid: int) -> WindowInfo | None:
    """The window a user would call "the app": visible, titled, biggest first."""
    candidates = [w for w in find_windows(pid=pid) if w.visible and w.title]
    if not candidates:
        return None
    candidates.sort(key=lambda w: (w.rect[2] - w.rect[0]) * (w.rect[3] - w.rect[1]), reverse=True)
    return candidates[0]


def is_foreground(hwnd: int) -> bool:
    return bool(IS_WINDOWS and user32.GetForegroundWindow() == hwnd)


def _foreground(hwnd: int) -> bool:
    """One ``SetForegroundWindow`` attempt with every thread attached.

    Windows refuses the call unless the caller owns the foreground, so both the
    target's thread *and* whoever currently owns the foreground are attached
    first: the lock is checked against the attached set, not against us.
    """
    fg = user32.GetForegroundWindow()
    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    this_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0

    attached: list[int] = []
    for tid in (target_tid, fg_tid):
        if tid and tid != this_tid and tid not in attached:
            if user32.AttachThreadInput(this_tid, tid, True):
                attached.append(tid)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd and user32.IsWindow(hwnd):
            # A real input event releases the foreground lock; a lone ALT tap is
            # the cheapest one and is a no-op for the editor.  This is the same
            # trick AltTab-style tools use.
            _send(_key_input(VK.MENU), _key_input(VK.MENU, up=True))
            user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() != hwnd and user32.IsWindow(hwnd):
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except AttributeError:  # pragma: no cover - pre-XP
                pass
    finally:
        for tid in attached:
            user32.AttachThreadInput(this_tid, tid, False)
    return user32.GetForegroundWindow() == hwnd


def focus(hwnd: int, timeout: float = 1.5) -> bool:
    """Bring ``hwnd`` to the foreground.  Returns whether it made it.

    Retried rather than tried once: the foreground can be held by a window that
    is closing, and a single refusal is not a verdict.
    """
    if not IS_WINDOWS or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE

    import time

    deadline = time.time() + max(0.05, timeout)
    while True:
        if user32.GetForegroundWindow() == hwnd:
            return True
        if _foreground(hwnd):
            return True
        if time.time() >= deadline:
            return user32.GetForegroundWindow() == hwnd
        time.sleep(0.1)


def show(hwnd: int, state: int = 9) -> bool:
    """``SW_RESTORE`` (9) / ``SW_MAXIMIZE`` (3) / ``SW_MINIMIZE`` (6)."""
    return bool(IS_WINDOWS and user32.ShowWindow(hwnd, state))


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_EXTENDEDKEY = 0x0001
_MAPVK_VK_TO_VSC = 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wt.ULONG)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("union", _INPUTUNION)]


def _send(*inputs: _INPUT) -> int:
    if not IS_WINDOWS:
        return 0
    array = (_INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), array, ctypes.sizeof(_INPUT)))


def _key_input(vk: int, up: bool = False) -> _INPUT:
    scan = user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)
    flags = _KEYEVENTF_KEYUP if up else 0
    if scan == 0 and vk not in (VK.SHIFT, VK.CONTROL, VK.MENU):  # pragma: no cover
        flags |= _KEYEVENTF_UNICODE if vk < 0x80 else 0
    return _INPUT(type=_INPUT_KEYBOARD, union=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, flags, 0, None)))


def press(keys: Sequence[int | str], hold: float = 0.02) -> bool:
    """Press a chord: ``press(["ctrl", "s"])`` or ``press([VK.CONTROL, ord("S")])``."""
    import time

    vks = [key_to_vk(k) if isinstance(k, str) else int(k) for k in keys]
    if not vks:
        return False
    for vk in vks:
        _send(_key_input(vk))
    time.sleep(max(0.0, hold))
    for vk in reversed(vks):
        _send(_key_input(vk, up=True))
    return True


def hotkey(spec: str, hold: float = 0.02) -> bool:
    """``"ctrl+shift+s"`` -> one chord."""
    parts = [p for p in str(spec).replace("-", "+").split("+") if p]
    if not parts:
        return False
    return press(parts, hold=hold)


def send_keys(keys: Iterable[int | str]) -> bool:
    """Type a sequence, one key at a time (chords must use :func:`press`)."""
    ok = True
    for key in keys:
        ok = press([key]) and ok
    return ok


def type_text(text: str, per_char_delay: float = 0.0) -> bool:
    """Type Unicode text into whatever has focus (no clipboard involved)."""
    import time

    if not IS_WINDOWS:
        return False
    for char in str(text):
        unit = ord(char)
        # Surrogate pairs arrive as one code point; SendInput wants UTF-16 units.
        if unit > 0xFFFF:
            unit -= 0x10000
            for half in (0xD800 + (unit >> 10), 0xDC00 + (unit & 0x3FF)):
                _send(
                    _INPUT(type=_INPUT_KEYBOARD, union=_INPUTUNION(ki=_KEYBDINPUT(0, half, _KEYEVENTF_UNICODE, 0, None))),
                    _INPUT(type=_INPUT_KEYBOARD, union=_INPUTUNION(ki=_KEYBDINPUT(0, half, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None))),
                )
        elif char == "\n":
            _send(_key_input(VK.RETURN), _key_input(VK.RETURN, up=True))
        elif char == "\t":
            _send(_key_input(VK.TAB), _key_input(VK.TAB, up=True))
        else:
            _send(
                _INPUT(type=_INPUT_KEYBOARD, union=_INPUTUNION(ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE, 0, None))),
                _INPUT(type=_INPUT_KEYBOARD, union=_INPUTUNION(ki=_KEYBDINPUT(0, unit, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP, 0, None))),
            )
        if per_char_delay:
            time.sleep(per_char_delay)
    return True


_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_RIGHTDOWN = 0x0008
_MOUSEEVENTF_RIGHTUP = 0x0010
_MOUSEEVENTF_MIDDLEDOWN = 0x0020
_MOUSEEVENTF_MIDDLEUP = 0x0040
_MOUSEEVENTF_WHEEL = 0x0800


def click_at(x: int, y: int, button: str = "left", clicks: int = 1, delay: float = 0.02) -> bool:
    """Real mouse click at screen coordinates (UIA fallback for canvas widgets)."""
    import time

    if not IS_WINDOWS:
        return False
    down, up = {
        "left": (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP),
        "right": (_MOUSEEVENTF_RIGHTDOWN, _MOUSEEVENTF_RIGHTUP),
        "middle": (_MOUSEEVENTF_MIDDLEDOWN, _MOUSEEVENTF_MIDDLEUP),
    }.get(button, (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP))
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.01)
    for _ in range(max(1, clicks)):
        user32.mouse_event(down, 0, 0, 0, 0)
        time.sleep(delay)
        user32.mouse_event(up, 0, 0, 0, 0)
        time.sleep(delay)
    return True


def move_to(x: int, y: int) -> bool:
    return bool(IS_WINDOWS and user32.SetCursorPos(int(x), int(y)))


def scroll(amount: int) -> bool:
    """Positive scrolls up; each notch is 120."""
    return bool(IS_WINDOWS and user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, int(amount), 0))


# --------------------------------------------------------------------------- #
# clipboard
# --------------------------------------------------------------------------- #
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def clipboard_get() -> str:
    if not IS_WINDOWS:
        return ""
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def clipboard_set(text: str) -> bool:
    if not IS_WINDOWS:
        return False
    data = ctypes.create_unicode_buffer(str(text))
    size = ctypes.sizeof(data)
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        kernel32.GlobalAlloc.restype = wt.HGLOBAL
        handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, size)
        finally:
            kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(_CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


# --------------------------------------------------------------------------- #
# processes
# --------------------------------------------------------------------------- #
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * _MAX_PATH),
    ]


def pids_of_images(images: Iterable[str]) -> list[int]:
    """Pids whose image name matches any of ``images`` (case-insensitive)."""
    if not IS_WINDOWS:
        return []
    wanted = {str(i).lower() for i in images if i}
    out: list[int] = []
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return []
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            if entry.szExeFile.lower() in wanted:
                out.append(int(entry.th32ProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return out


def pid_of_image(*images: str) -> int:
    found = pids_of_images(images)
    return found[0] if found else 0


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WOW64_ARCH_MACHINE = 0xFFFF


def is_64bit(pid: int) -> bool:
    """Whether ``pid`` is a 64-bit process (matters before injecting a DLL).

    ``IsWow64Process2`` reports the *guest* machine: 0 means "not WOW64", i.e.
    the process runs on the native architecture.  ``IsWow64Process`` is the
    fallback for pre-1511 systems.
    """
    if not IS_WINDOWS or not pid:
        return os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64").endswith("64")
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return True
    try:
        machine = wt.USHORT()
        if kernel32.IsWow64Process2(handle, ctypes.byref(machine), None):
            return machine.value == 0
        wow = wt.BOOL()
        if kernel32.IsWow64Process(handle, ctypes.byref(wow)):
            return not bool(wow.value)
        return True
    finally:
        kernel32.CloseHandle(handle)


def image_path(pid: int) -> str:
    """Full path of the executable behind ``pid``, or ``""`` when unreadable.

    The two Synthesizer V generations both ship ``synthv-studio.exe``, so the
    image *name* cannot tell them apart - only the folder it was started from.
    """
    if not IS_WINDOWS or not pid:
        return ""
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wt.DWORD(_MAX_PATH * 2)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)
