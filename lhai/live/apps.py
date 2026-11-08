"""Which editor is running, and what can we do to it right now.

A single place that answers "is CeVIO up, what is its window handle, and which
control channel should I use".  It sits above :mod:`lhai.mcp.engines.base`
(install discovery) and below the MCP tools, so the policy - prefer COM, then
Lua, then an injected agent, then UI Automation - is stated once.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

from lhai.live import agent as agent_mod
from lhai.live import inject as inject_mod
from lhai.live import win32
from lhai.mcp.engines import base

__all__ = [
    "ENGINE_KEYS",
    "IMAGES",
    "LiveEngine",
    "find",
    "find_all",
    "status",
    "launch",
    "focus",
    "close",
]

ENGINE_KEYS: tuple[str, ...] = ("cevio", "voisona", "synthv", "vocaloid")

#: Image names as they appear in the task list.  CeVIO AI ships a patched
#: ``CeVIO AI.orig.exe`` on some installs, so both spellings are listed.
IMAGES: dict[str, tuple[str, ...]] = {
    "cevio": ("CeVIO AI.exe", "CeVIO AI.orig.exe", "CeVIO Creative Studio.exe"),
    "voisona": ("VoiSona.exe",),
    "synthv": ("synthv-studio.exe",),
    "vocaloid": ("VOCALOID6.exe", "VOCALOID5.exe", "VOCALOID4.exe"),
}

#: The channel that actually works per engine, best first.  ``agent`` means the
#: injected DLL; ``uia``/``keys`` mean window automation.
CHANNELS: dict[str, tuple[str, ...]] = {
    "cevio": ("com", "uia", "keys", "agent"),
    "voisona": ("uia", "keys", "agent"),
    "synthv": ("lua", "uia", "keys", "agent"),
    "vocaloid": ("uia", "keys", "agent"),
}

#: One sentence per engine, shown to the model so it stops asking for a channel
#: that does not exist (there is no VOCALOID scripting API, and pretending is
#: worse than saying so).
NOTES: dict[str, str] = {
    "cevio": "Talk has a real COM API (CeVIO.Talk.RemoteService2); song editing is UI automation + .ccs reload",
    "voisona": "no scripting API - UI Automation, hotkeys and .svp/.ccs import; the agent DLL gives memory-level access",
    "synthv": "resident Lua agent edits the open project live; .svp writer works without it",
    "vocaloid": "no scripting API - UI Automation/hotkeys plus the injected agent for pattern-scanned model access",
}


@dataclass(slots=True)
class LiveEngine:
    key: str
    label: str
    installed: bool
    exe: str
    pid: int = 0
    hwnd: int = 0
    title: str = ""
    agent_loaded: bool = False
    channels: tuple[str, ...] = ()
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.pid > 0

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "engine": self.key,
            "label": self.label,
            "installed": self.installed,
            "running": self.running,
            "pid": self.pid,
            "hwnd": self.hwnd,
            "title": self.title,
            "channels": list(self.channels),
            "note": self.note,
            "agent_loaded": self.agent_loaded,
        }
        if self.exe:
            payload["exe"] = self.exe
        if self.extra:
            payload["extra"] = self.extra
        return payload


def find(key: str) -> LiveEngine:
    """Locate ``key`` and, if it is running, attach to its main window."""
    if key not in ENGINE_KEYS:
        raise KeyError(f"unknown engine {key!r}; expected one of {ENGINE_KEYS}")
    install = base.find_engine(key)
    live = LiveEngine(
        key=key,
        label=install.label,
        installed=install.installed,
        exe=str(install.exe) if install.exe else "",
        channels=CHANNELS[key],
        note=NOTES[key],
    )
    pids = win32.pids_of_images(IMAGES[key])
    if pids:
        live.pid = pids[0]
        window = win32.window_for_pid(live.pid)
        if window is not None:
            live.hwnd = window.hwnd
            live.title = window.title
        live.agent_loaded = inject_mod.is_loaded(live.pid)
        if len(pids) > 1:
            live.extra["other_pids"] = pids[1:]
    return live


def find_all() -> list[LiveEngine]:
    return [find(k) for k in ENGINE_KEYS]


def status() -> dict[str, Any]:
    return {
        "engines": [e.as_dict() for e in find_all()],
        "input_ok": sys.platform == "win32",
    }


def launch(key: str, file: str = "") -> dict[str, Any]:
    """Start the editor (optionally with a project) and wait for its window."""
    import time

    live = find(key)
    if live.running:
        return {"ok": True, "already_running": True, **live.as_dict()}
    result = base.launch(key, file or None)
    if not result.get("launched"):
        return {"ok": False, **result}
    deadline = time.time() + 20.0
    while time.time() < deadline:
        refreshed = find(key)
        if refreshed.hwnd:
            return {"ok": True, **refreshed.as_dict(), "launch": result}
        time.sleep(0.4)
    return {"ok": False, "error": "process started but no window appeared", "launch": result}


def focus(key: str) -> dict[str, Any]:
    live = find(key)
    if not live.hwnd:
        return {"ok": False, "error": f"{live.label} is not running", "engine": key}
    ok = win32.focus(live.hwnd)
    return {"ok": ok, "engine": key, "hwnd": live.hwnd, "title": live.title}


def close(key: str, force: bool = False) -> dict[str, Any]:
    """Ask the window to close (``WM_CLOSE``), or kill the process."""
    live = find(key)
    if not live.running:
        return {"ok": True, "engine": key, "already_stopped": True}
    if live.hwnd and not force:
        win32.user32.PostMessageW(live.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return {"ok": True, "engine": key, "pid": live.pid, "method": "wm_close"}
    try:
        subprocess.run(["taskkill", "/PID", str(live.pid), "/F"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "engine": key, "error": str(exc)}
    return {"ok": True, "engine": key, "pid": live.pid, "method": "taskkill"}


def ensure_agent(key: str, auto_inject: bool = True) -> dict[str, Any]:
    live = find(key)
    if not live.running:
        return {"ok": False, "error": f"{live.label} is not running", "engine": key}
    result = agent_mod.ensure(live.pid, auto_inject=auto_inject)
    return {"engine": key, **result}
