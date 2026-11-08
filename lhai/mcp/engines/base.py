"""Engine discovery and launch for CeVIO AI, VoiSona, Synthesizer V and VOCALOID.

None of these four ships a documented automation API for score editing, so the
integration strategy differs per engine and is stated honestly rather than
faked behind a uniform method that would fail at runtime:

==================  ==========================================================
engine              editable channel
==================  ==========================================================
Synthesizer V       ``.svp`` JSON (fully readable/writable) **and** the Lua
                    script API, which is a real live channel: a resident script
                    can poll a command file and edit the open project.
VOCALOID            ``.vsqx`` XML (schema ``vsq4.xsd`` ships with the editor)
                    and MIDI import. VOCALOID6 has no external API.
CeVIO AI            ``.ccs`` XML project and MIDI/MusicXML import.
VoiSona             imports ``.svp`` / ``.ccs`` / MIDI / MusicXML directly.
==================  ==========================================================

Discovery is deliberately boring: a fixed candidate list plus an uninstall
registry scan plus an explicit environment override.  Nothing guesses from a
window title - if the exe is not found, :func:`find_engine` says so and returns
``None``, and the caller degrades to "write the file, open it yourself".
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lhai.mcp.ir import Song

__all__ = [
    "EngineInstall",
    "ENGINE_KEYS",
    "ENGINE_LABELS",
    "find_engine",
    "find_all",
    "launch",
    "is_running",
    "open_in_engine",
]

ENGINE_KEYS: tuple[str, ...] = ("synthv", "vocaloid", "cevio", "voisona")

ENGINE_LABELS: dict[str, str] = {
    "synthv": "Synthesizer V Studio",
    "vocaloid": "VOCALOID",
    "cevio": "CeVIO AI",
    "voisona": "VoiSona",
}

#: Env override, e.g. ``LHAI_SYNTHV_EXE=D:\Synthesizer V Studio 2 Pro\synthv-studio.exe``.
_ENV_OVERRIDE = {k: f"LHAI_{k.upper()}_EXE" for k in ENGINE_KEYS}

#: Ordered candidates.  The first existing path wins, so the newest install can
#: be listed first without any version parsing.
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "synthv": (
        r"D:\Synthesizer V Studio 2 Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio 2 Pro\synthv-studio.exe",
        r"D:\Synthesizer V Studio Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio Basic\synthv-studio.exe",
        r"D:\Synthesizer V Studio Flat\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio\synthv-studio.exe",
        r"D:\Synthesizer V Studio\synthv-studio.exe",
        r"C:\Program Files (x86)\Synthesizer V Studio\synthv-studio.exe",
    ),
    "vocaloid": (
        r"D:\VOCALOID6\Editor\VOCALOID6.exe",
        r"C:\Program Files\VOCALOID6\Editor\VOCALOID6.exe",
        r"C:\Program Files\VOCALOID5\Editor\VOCALOID5.exe",
        r"C:\Program Files (x86)\VOCALOID5\Editor\VOCALOID5.exe",
        r"C:\Program Files (x86)\VOCALOID4\Editor\VOCALOID4.exe",
    ),
    "cevio": (
        r"C:\Program Files\CeVIO\CeVIO AI\CeVIO AI.exe",
        r"C:\Program Files\CeVIO\CeVIO Creative Studio\CeVIO Creative Studio.exe",
        r"D:\CeVIO\CeVIO AI\CeVIO AI.exe",
    ),
    "voisona": (
        r"D:\VoiSona118\VoiSona.exe",
        r"C:\Program Files\VoiSona\VoiSona.exe",
        r"C:\Program Files (x86)\VoiSona\VoiSona.exe",
        r"D:\VoiSona\VoiSona.exe",
    ),
}

#: Registry DisplayName fragments, used when the install went somewhere else.
_REGISTRY_HINTS: dict[str, tuple[str, ...]] = {
    "synthv": ("synthesizer v",),
    "vocaloid": ("vocaloid",),
    "cevio": ("cevio",),
    "voisona": ("voisona",),
}

#: Extensions each engine can open, in preference order.  VoiSona's own list
#: comes from the strings in ``VoiSona.exe``: it imports ``.svp``, ``.ccs``,
#: ``.mid`` and MusicXML, which is why it needs no writer of its own.
FORMATS: dict[str, tuple[str, ...]] = {
    "synthv": (".svp",),
    "vocaloid": (".vsqx", ".mid"),
    "cevio": (".ccs", ".mid"),
    "voisona": (".svp", ".ccs", ".mid"),
}

_PROCESS_NAMES: dict[str, str] = {
    "synthv": "synthv-studio.exe",
    "vocaloid": "VOCALOID6.exe",
    "cevio": "CeVIO AI.exe",
    "voisona": "VoiSona.exe",
}


@dataclass(slots=True)
class EngineInstall:
    """One detected engine.  ``exe is None`` means "not installed / not found"."""

    key: str
    label: str
    exe: Path | None = None
    root: Path | None = None
    formats: tuple[str, ...] = ()
    #: How to control it, in one sentence - shown to the model so it does not
    #: try a live method that this engine does not have.
    channel: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def installed(self) -> bool:
        return self.exe is not None and self.exe.exists()

    def as_dict(self) -> dict[str, Any]:
        return {
            "engine": self.key,
            "label": self.label,
            "installed": self.installed,
            "exe": str(self.exe) if self.exe else "",
            "root": str(self.root) if self.root else "",
            "formats": list(self.formats),
            "channel": self.channel,
            "extra": self.extra,
        }


_CHANNELS: dict[str, str] = {
    "synthv": "native .svp writer + resident Lua agent for live edits",
    "vocaloid": "native .vsqx writer + MIDI import; live: UIA/keys + injected agent",
    "cevio": "native .ccs writer + MIDI import; talk has COM (CeVIO.Talk.RemoteService2)",
    "voisona": "imports .svp/.ccs/.mid; live: UIA/keys + injected agent",
}


def _from_registry(key: str) -> Path | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows
        return None

    uninstall = r"Microsoft\Windows\CurrentVersion\Uninstall"
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + uninstall),
        (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + uninstall),
        (winreg.HKEY_CURRENT_USER, "SOFTWARE\\" + uninstall),
    )
    hints = _REGISTRY_HINTS[key]
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as root:
                count = winreg.QueryInfoKey(root)[0]
                for i in range(count):
                    try:
                        name = winreg.EnumKey(root, i)
                        with winreg.OpenKey(root, name) as entry:
                            display = str(_reg_value(entry, "DisplayName") or "").lower()
                            if not any(h in display for h in hints):
                                continue
                            location = _reg_value(entry, "InstallLocation")
                            if location and Path(location).exists():
                                return Path(location)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _reg_value(entry: Any, name: str) -> Any:
    try:
        return entry and __import__("winreg").QueryValueEx(entry, name)[0]
    except OSError:
        return None


def _exe_in(root: Path, key: str) -> Path | None:
    names = {
        "synthv": ("synthv-studio.exe",),
        "vocaloid": ("VOCALOID6.exe", "VOCALOID5.exe", "VOCALOID4.exe"),
        "cevio": ("CeVIO AI.exe", "CeVIO Creative Studio.exe"),
        "voisona": ("VoiSona.exe",),
    }[key]
    direct = [root / n for n in names] + [root / "Editor" / n for n in names]
    for candidate in direct:
        if candidate.exists():
            return candidate
    for pattern in names:
        try:
            for found in root.rglob(pattern):
                return found
        except OSError:
            continue
    return None


def find_engine(key: str) -> EngineInstall:
    """Locate one engine.  Never raises; ``installed`` reports the truth."""
    if key not in ENGINE_KEYS:
        raise KeyError(f"unknown engine {key!r}; expected one of {ENGINE_KEYS}")
    label = ENGINE_LABELS[key]
    override = os.environ.get(_ENV_OVERRIDE[key])
    if override:
        exe = Path(os.path.expandvars(override))
        if exe.exists():
            return EngineInstall(
                key=key, label=label, exe=exe, root=exe.parent, formats=FORMATS[key],
                channel=_CHANNELS[key], extra={"discovered_by": "env"},
            )

    for raw in _CANDIDATES[key]:
        exe = Path(os.path.expandvars(raw))
        if exe.exists():
            return EngineInstall(
                key=key, label=label, exe=exe, root=exe.parent, formats=FORMATS[key],
                channel=_CHANNELS[key], extra={"discovered_by": "candidate"},
            )

    location = _from_registry(key)
    if location is not None:
        exe = _exe_in(location, key)
        if exe is not None:
            return EngineInstall(
                key=key, label=label, exe=exe, root=exe.parent, formats=FORMATS[key],
                channel=_CHANNELS[key], extra={"discovered_by": "registry"},
            )

    found = _scan_roots(key)
    if found is not None:
        return EngineInstall(
            key=key, label=label, exe=found, root=found.parent, formats=FORMATS[key],
            channel=_CHANNELS[key], extra={"discovered_by": "scan"},
        )

    return EngineInstall(key=key, label=label, formats=FORMATS[key], channel=_CHANNELS[key])


def _scan_roots(key: str) -> Path | None:
    """Last resort: a shallow scan of the usual install roots.

    Depth-limited on purpose - a full ``D:\\`` walk on a machine with a few
    hundred thousand files takes minutes, and this runs on every tool call that
    needs the engine.
    """
    roots = [
        Path(r"C:\Program Files"),
        Path(r"C:\Program Files (x86)"),
        Path("D:/"),
    ]
    names = {
        "synthv": "synthv-studio.exe",
        "vocaloid": "VOCALOID6.exe",
        "cevio": "CeVIO AI.exe",
        "voisona": "VoiSona.exe",
    }[key]
    for root in roots:
        if not root.exists():
            continue
        try:
            for parent in root.iterdir():
                if not parent.is_dir():
                    continue
                lowered = parent.name.lower()
                if not any(h in lowered for h in _REGISTRY_HINTS[key]):
                    continue
                hit = _exe_in(parent, key)
                if hit is not None:
                    return hit
        except OSError:
            continue
    return None


def find_all() -> list[EngineInstall]:
    return [find_engine(k) for k in ENGINE_KEYS]


def is_running(key: str) -> bool:
    """Best-effort process check (``tasklist``, no psutil dependency)."""
    if sys.platform != "win32":
        return False
    image = _PROCESS_NAMES.get(key, "")
    if not image:
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return False
    return image.lower() in out.lower()


def launch(key: str, file: str | Path | None = None) -> dict[str, Any]:
    """Start the engine, optionally with a project file.  Does not wait."""
    install = find_engine(key)
    if not install.installed or install.exe is None:
        return {
            "launched": False,
            "engine": key,
            "reason": f"{install.label} not found; set {_ENV_OVERRIDE[key]} to its exe",
        }
    args = [str(install.exe)]
    if file is not None:
        target = Path(file)
        if not target.exists():
            return {"launched": False, "engine": key, "reason": f"{target} does not exist"}
        args.append(str(target))
    try:
        proc = subprocess.Popen(args, cwd=str(install.exe.parent), close_fds=True)
    except OSError as exc:
        return {"launched": False, "engine": key, "reason": f"{type(exc).__name__}: {exc}"}
    return {
        "launched": True,
        "engine": key,
        "pid": proc.pid,
        "exe": str(install.exe),
        "file": str(file) if file else "",
    }


def open_in_engine(key: str, file: str | Path) -> dict[str, Any]:
    """Open ``file`` with the engine that can read that extension."""
    return launch(key, file)


def preferred_extension(key: str, song: Song | None = None) -> str:
    """Which native file this engine should be handed.

    VoiSona prefers ``.svp`` because it carries phonemes and control curves
    better than MIDI; VOCALOID prefers ``.vsqx`` for the same reason.
    """
    formats = FORMATS[key]
    return formats[0]
