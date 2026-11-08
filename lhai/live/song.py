"""Song-side (singing synthesis) live control of the four editors.

`apps.py` answers "is the editor up".  `agent.py` answers "can I reach its
memory".  This module answers the only question that matters for singing
synthesis: **what can I actually change in a running song, right now.**

Four engines, four very different amounts of surface, and the honest answer is
not the same for any two of them:

===========  ===========================================================
engine       live song surface
===========  ===========================================================
``synthv``   a sanctioned in-process Lua API - notes, lyrics, phonemes,
             pitch, tracks, transport, one undo block per batch
``cevio``    no API at all; UI Automation exposes the Song menu (bulk lyric
             entry, quantize, measures), the transport, the tool palette and
             the six parameter lanes (volume / pitch / vibrato / alpha /
             husky / timing), plus tempo and quantize readouts
``vocaloid`` same shape as CeVIO, different control ids
``voisona``  same shape again
===========  ===========================================================

So this module is built around a **control map**: a per-engine dictionary from a
stable name (``"play"``, ``"pen"``, ``"pitch"``) to the automation id of the
widget that carries it.  CeVIO's map was read off a running instance and is
shipped.  VOCALOID's and VoiSona's are not guessed - :func:`learn` walks their
toolbar and saves what it finds, because a wrong id is worse than a missing one:
it clicks something else.

Two rules the callers depend on:

* **every call reports what it did**, including *why* it could not: a disabled
  button (no song track yet), a missing control map, or an element that has no
  invoke pattern are three different answers and are returned as three.
* **state is read back, not assumed.**  Clicking a radio button is not evidence
  it became the active one, so the toggle/selection state is re-read and
  returned.  That is what makes a batch verifiable without a human looking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import apps, uia, win32

__all__ = [
    "ENGINES",
    "CAPABILITIES",
    "caps",
    "learn",
    "control_map",
    "activate",
    "transport",
    "tool",
    "lane",
    "readouts",
    "modes",
    "lyrics",
    "undo",
    "redo",
    "save",
    "snapshot",
    "open_song",
]

ENGINES = ("synthv", "cevio", "voisona", "vocaloid")


# --------------------------------------------------------------------------- #
# what each engine can actually do
# --------------------------------------------------------------------------- #
#: ``channel`` is the strongest live route; ``notes`` / ``lyrics`` / ``pitch``
#: name the *mechanism*, so "no" and "via file reload" are never confused.
CAPABILITIES: dict[str, dict[str, Any]] = {
    "synthv": {
        "label": "Synthesizer V Studio",
        "channel": "lua",
        "live": True,
        "notes": "read+write (Lua)",
        "lyrics": "per note (Lua)",
        "phonemes": "per note (Lua)",
        "pitch": "offset per note (Lua)",
        "tracks": "select + list (Lua)",
        "transport": "play / stop / seek (Lua)",
        "undo": "one block per batch (Lua)",
        "lanes": [],
        "tools": [],
        "note": "the only engine with a sanctioned in-process API",
    },
    "cevio": {
        "label": "CeVIO AI",
        "channel": "uia",
        "live": True,
        "notes": "file open (.ccs) - the score is not a control",
        "lyrics": "bulk entry dialog (F8, live)",
        "phonemes": "lyrics-with-phoneme dialog (Ctrl+P, live)",
        "pitch": "parameter lane + pen (live)",
        "tracks": "Song menu / toolbar (live)",
        "transport": "play / stop / head / markers (live)",
        "undo": "Ctrl+Z on the editor undo stack",
        "lanes": ["normal", "timing", "volume", "pitch", "vibrato", "alpha", "husky"],
        "tools": ["arrow", "bulk", "pen", "line", "eraser"],
        "note": "song has no API; UIA carries the Song menu, transport, tools and lanes",
    },
    "vocaloid": {
        "label": "VOCALOID6",
        "channel": "uia",
        "live": True,
        "notes": "file open (.vsqx) - the score is not a control",
        "lyrics": "clipboard paste onto a selection (live)",
        "phonemes": "clipboard paste (live)",
        "pitch": "parameter lane + pen (live)",
        "tracks": "track menu (live)",
        "transport": "play / stop / return to top (live)",
        "undo": "Ctrl+Z on the editor undo stack",
        "lanes": [],
        "tools": [],
        "note": "control ids are per build: run learn('vocaloid') first",
    },
    "voisona": {
        "label": "VoiSona",
        "channel": "uia",
        "live": True,
        "notes": "file open (.svp / .ccs) - the score is not a control",
        "lyrics": "clipboard paste onto a selection (live)",
        "phonemes": "clipboard paste (live)",
        "pitch": "parameter lane + pen (live)",
        "tracks": "track menu (live)",
        "transport": "play / stop (live)",
        "undo": "Ctrl+Z on the editor undo stack",
        "lanes": [],
        "tools": [],
        "note": "control ids are per build: run learn('voisona') first",
    },
}


def caps(engine: str = "") -> dict[str, Any]:
    """The capability table, for one engine or all of them."""
    if engine:
        return CAPABILITIES.get(engine, {"error": f"unknown engine {engine!r}"})
    return {k: dict(v) for k, v in CAPABILITIES.items()}


# --------------------------------------------------------------------------- #
# control maps
# --------------------------------------------------------------------------- #
#: Read off a running CeVIO AI 9.1.14 (automation ids are stable per major
#: version and per install language, which is why they are worth shipping).
_CEVIO: dict[str, dict[str, str]] = {
    "transport": {
        "head": "headButton",
        "play": "playButton",
        "stop": "stopButton",
        "repeat": "repeatButton",
        "marker_start": "markerSButton",
        "marker_end": "markerEButton",
        "autoscroll": "autoScrollButton",
    },
    "tools": {
        "arrow": "arrowButton",
        "bulk": "bulkSelectionButton",
        "pen": "penButton",
        "line": "lineButton",
        "eraser": "eraserButton",
    },
    "lanes": {
        "normal": "norButton",
        "timing": "tmgButton",
        "volume": "volButton",
        "pitch": "pitButton",
        "vibrato": "viaButton",
        "alpha": "alpButton",
        "husky": "husButton",
    },
    "readouts": {
        "time": "timeText",
        "tempo": "tempoText",
        "beat": "beatText",
        "quantize": "quantizeText",
    },
    "panes": {"tracks": "trackViewer", "lanes": "laneViewer"},
}

BUILTIN: dict[str, dict[str, dict[str, str]]] = {"cevio": _CEVIO}

#: Where a learned map is cached, next to the rest of the workspace state.
def map_path(engine: str) -> Path:
    base = Path.home() / "AppData" / "Local" / "lh-ai-studio" / "singing" / "ui"
    return base / f"{engine}.json"


def control_map(engine: str, reload: bool = False) -> dict[str, dict[str, str]]:
    """Learned map if one was saved, else the built-in one, else empty."""
    cached = map_path(engine)
    if not reload and cached.exists():
        try:
            data = json.loads(cached.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data:
                return data
        except (OSError, ValueError):
            pass
    return {k: dict(v) for k, v in BUILTIN.get(engine, {}).items()}


#: Suffixes/prefixes that identify a control by name when no map exists yet.
_PATTERNS: dict[str, tuple[str, ...]] = {
    "transport": ("play", "stop", "head", "return", "top", "repeat", "loop", "marker"),
    "tools": ("pen", "arrow", "select", "eraser", "line", "pencil", "draw"),
    "lanes": ("vol", "pit", "via", "alp", "hus", "tmg", "timing", "parameter"),
    "readouts": ("tempo", "beat", "time", "quantize", "grid", "bpm"),
    "panes": ("track", "lane", "piano", "roll", "note"),
}

#: Short vendor ids expanded to the musical name a caller would guess.
_ALIASES: dict[str, str] = {
    "vol": "volume",
    "pit": "pitch",
    "via": "vibrato",
    "alp": "alpha",
    "hus": "husky",
    "tmg": "timing",
    "nor": "normal",
    "markers": "marker_start",
}


def _control_name(automation_id: str) -> str:
    """``pitButton`` -> ``pitch``: a stable name a caller can guess."""
    name = automation_id
    for suffix in ("RadioButton", "ToggleButton", "Button", "Text", "Viewer", "Panel"):
        if name.lower().endswith(suffix.lower()):
            name = name[: -len(suffix)]
            break
    name = name.strip("_") or automation_id
    key = name.lower()
    return _ALIASES.get(key, key)


def learn(engine: str, save: bool = True) -> dict[str, Any]:
    """Walk a running editor's toolbar and record which control is which.

    Ids are matched case-insensitively against the id, the name and the class
    name, and only inside a *visible* element with a clickable rectangle - a
    hidden template or a collapsed panel is not a control you can press, and
    recording one produces clicks that silently do nothing.

    The key is derived from the id (``pitButton`` -> ``pitch``) and the raw id is
    kept in the same table, so a caller can ask by either name.
    """
    live = apps.find(engine)
    if live is None or not live.running:
        return {"ok": False, "engine": engine, "error": "not running"}
    tree = uia.dump_tree(live.hwnd, depth=16, max_nodes=4000)
    if tree is None:
        return {"ok": False, "engine": engine, "error": "no automation tree"}

    flat: list[Any] = []

    def walk(el: Any) -> None:
        flat.append(el)
        for child in el.children or []:
            walk(child)

    walk(tree)

    found: dict[str, dict[str, str]] = {}
    for bucket, needles in _PATTERNS.items():
        hits: dict[str, str] = {}
        for el in flat:
            aid = (el.automation_id or "").strip()
            if not aid or aid in ("image", "Image"):
                continue
            hay = f"{aid} {el.name or ''} {el.class_name or ''}".lower()
            if not any(n in hay for n in needles):
                continue
            if el.offscreen:
                continue
            left, top, right, bottom = el.rect
            if right <= left or bottom <= top:
                continue
            hits.setdefault(_control_name(aid), aid)
            hits.setdefault(aid, aid)
        if hits:
            found[bucket] = dict(sorted(hits.items()))

    if not found:
        return {"ok": False, "engine": engine, "error": "nothing matched - is a project open?"}

    if save:
        path = map_path(engine)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(found, indent=1, sort_keys=True), encoding="utf-8")
    return {"ok": True, "engine": engine, "map": found, "saved": str(map_path(engine)) if save else ""}


# --------------------------------------------------------------------------- #
# acting on a control
# --------------------------------------------------------------------------- #
def _live(engine: str) -> tuple[Any | None, dict[str, Any] | None]:
    try:
        live = apps.find(engine)
    except KeyError:
        return None, {"ok": False, "error": f"unknown engine {engine!r}", "known": list(ENGINES)}
    if live is None:
        return None, {"ok": False, "error": f"unknown engine {engine!r}", "known": list(ENGINES)}
    if not live.running:
        return None, {"ok": False, "engine": engine, "error": "not running", "hint": f"live_launch {engine}"}
    return live, None


def activate(engine: str, bucket: str, name: str, focus_first: bool = True) -> dict[str, Any]:
    """Press one mapped control and read its new state back.

    Focus is taken first for the engines whose UIA invoke is a no-op (some WPF
    radio buttons only respond to a real click), and the state is read back so a
    caller can tell "pressed and it took" from "pressed and nothing happened".
    """
    live, problem = _live(engine)
    if problem:
        return problem
    mapping = control_map(engine).get(bucket) or {}
    aid = mapping.get(name)
    if not aid:
        return {
            "ok": False,
            "engine": engine,
            "error": f"no control mapped for {bucket}.{name}",
            "mapped": sorted(mapping),
            "hint": f"live_song_learn {engine}",
        }

    if focus_first:
        win32.focus(live.hwnd, timeout=2.0)

    el = uia.find(live.hwnd, automation_id=aid, timeout=1.5) or uia.find(
        live.hwnd, name=name, timeout=0.0
    )
    if el is None:
        return {"ok": False, "engine": engine, "automation_id": aid, "error": "control not found in tree"}

    info = uia.describe(el, 0)
    if not info.enabled:
        return {
            "ok": False,
            "engine": engine,
            "automation_id": aid,
            "error": "control is disabled",
            "hint": "CeVIO disables the parameter lanes and tools until a song track exists",
        }

    action = uia.invoke(el)
    state = uia.state_of(el)
    return {
        "ok": bool(action.get("ok")),
        "engine": engine,
        "bucket": bucket,
        "control": name,
        "automation_id": aid,
        "via": action.get("via", ""),
        "active": state.get("on"),
    }


def transport(engine: str, action: str) -> dict[str, Any]:
    """``play`` / ``stop`` / ``head`` / ``repeat`` / ``marker_start`` / ``marker_end``.

    Synthesizer V routes through its Lua agent; the other three through their
    toolbar.  ``rewind`` and ``forward`` are accepted as aliases because that is
    what every DAW calls them.
    """
    action = {"rewind": "head", "return": "head", "top": "head", "toggle": "play"}.get(action, action)
    if engine == "synthv":
        from . import synthv_live

        verb = {"play": "PLAY", "stop": "STOP", "head": "SEEK 0"}.get(action)
        if verb is None:
            return {"ok": False, "engine": engine, "error": f"synthv has no transport action {action!r}"}
        return synthv_live.send(_synthv_ipc(), [verb])
    return activate(engine, "transport", action)


def tool(engine: str, name: str) -> dict[str, Any]:
    """Select an edit tool: ``arrow`` / ``pen`` / ``line`` / ``eraser`` / ``bulk``."""
    return activate(engine, "tools", name)


def lane(engine: str, name: str) -> dict[str, Any]:
    """Switch the visible parameter lane.

    This is the singing-synthesis part of the surface that has nothing to do
    with talk: volume, **pitch**, **vibrato**, alpha, husky and timing are the
    curves a vocal is actually shaped with.
    """
    return activate(engine, "lanes", name)


def readouts(engine: str) -> dict[str, Any]:
    """Read tempo / time signature / position / quantize off the toolbar."""
    live, problem = _live(engine)
    if problem:
        return problem
    mapping = control_map(engine).get("readouts") or {}
    values: dict[str, str] = {}
    for key, aid in mapping.items():
        el = uia.find(live.hwnd, automation_id=aid, timeout=0.5)
        if el is not None:
            values[key] = uia.get_text(el)
    return {"ok": bool(values), "engine": engine, "values": values}


def modes(engine: str) -> dict[str, Any]:
    """Which tool, lane and transport mode are *currently* active.

    Reading this back is what turns a click into evidence, and it also answers
    "what is the editor's state" without saving or parsing anything.
    """
    live, problem = _live(engine)
    if problem:
        return problem
    mapping = control_map(engine)
    out: dict[str, Any] = {"ok": True, "engine": engine}
    for bucket in ("tools", "lanes", "transport"):
        active: list[str] = []
        present: list[str] = []
        for name, aid in (mapping.get(bucket) or {}).items():
            el = uia.find(live.hwnd, automation_id=aid, timeout=0.3)
            if el is None:
                continue
            present.append(name)
            state = uia.state_of(el)
            if state.get("ok") and state.get("on"):
                active.append(name)
        out[bucket] = {"active": active, "present": present}
    return out


# --------------------------------------------------------------------------- #
# lyrics - the one musical edit that is genuinely live on every engine
# --------------------------------------------------------------------------- #
def lyrics(engine: str, text: str, phonemes: bool = False, timeout: float = 8.0) -> dict[str, Any]:
    """Put a lyric block onto the selected notes, without leaving the editor.

    CeVIO and VOCALOID both fill a *selection* from one dialog, which is why
    this is the single most useful live call for singing synthesis: notes can
    come in from a file or a MIDI import, and the words arrive through the UI.

    ``text`` is split on whitespace and on newlines - one syllable per note, in
    order - so a caller passes ``"so ra wo to be"`` or one word per line.
    """
    # Reject unusable input before touching the editor: an empty block is a
    # caller mistake, and answering "the app is not running" to it sends the
    # caller off to start an editor that was never the problem.
    syllables = [w for w in text.replace("\r", "\n").replace("\n", " ").split(" ") if w]
    if not syllables:
        return {"ok": False, "engine": engine, "error": "no syllables in text"}
    live, problem = _live(engine)
    if problem:
        return problem
    if engine == "synthv":
        return {
            "ok": False,
            "engine": "synthv",
            "error": "SynthV takes lyrics per note",
            "use": "synthv_live_cmd(['LYRIC <index> <word>', ...])",
        }

    win32.focus(live.hwnd, timeout=2.0)
    win32.clipboard_set(" ".join(syllables))
    opened = win32.hotkey("ctrl+p") if phonemes else win32.press([0x77])  # F8
    if not opened:
        return {"ok": False, "engine": engine, "error": "could not send the lyric-entry shortcut"}

    import time

    deadline = time.time() + max(1.0, timeout)
    field = None
    while time.time() < deadline and field is None:
        time.sleep(0.4)
        tops = [w for w in win32.list_windows(visible_only=True) if w.pid == live.pid]
        for win in tops:
            if win.hwnd == live.hwnd:
                continue
            field = uia.find(win.hwnd, role="Edit", timeout=0.2) or uia.find(
                win.hwnd, role="Document", timeout=0.2
            )
            if field is None:
                # An unnamed multiline box is still an edit control.
                edits = uia.find_all(win.hwnd, role="Edit", limit=4)
                field = edits[0] if edits else None
            if field is not None:
                dialog = win
                break
    if field is None:
        win32.press([0x1B])
        return {
            "ok": False,
            "engine": engine,
            "error": "no lyric dialog appeared",
            "hint": "select the notes to fill first (arrow tool, then drag or Ctrl+A)",
        }

    set_result = uia.set_text(field, " ".join(syllables))
    if not set_result.get("ok"):
        win32.type_text(" ".join(syllables))
    win32.press([0x0D])  # Enter accepts the dialog
    time.sleep(0.6)
    still = [w for w in win32.list_windows(visible_only=True) if w.hwnd == dialog.hwnd]
    return {
        "ok": True,
        "engine": engine,
        "syllables": len(syllables),
        "dialog_closed": not still,
        "via": set_result.get("via", "keyboard"),
    }


def undo(engine: str) -> dict[str, Any]:
    return _key(engine, "ctrl+z")


def redo(engine: str) -> dict[str, Any]:
    return _key(engine, "ctrl+y")


def save(engine: str) -> dict[str, Any]:
    return _key(engine, "ctrl+s")


def _key(engine: str, spec: str) -> dict[str, Any]:
    live, problem = _live(engine)
    if problem:
        return problem
    win32.focus(live.hwnd, timeout=2.0)
    return {"ok": bool(win32.hotkey(spec)), "engine": engine, "keys": spec}


# --------------------------------------------------------------------------- #
# the score itself: read it back, or hand a new one over
# --------------------------------------------------------------------------- #
def snapshot(engine: str, path: str = "") -> dict[str, Any]:
    """Read the *open* song back out of the editor.

    The score is not a control on three of the four engines, so the honest way to
    read it is the editor's own save: ``Ctrl+S`` writes the project the engine
    itself would write, and the reader we already ship parses it.  Nothing is
    transcribed by hand and nothing is guessed.

    ``path`` is required when the project has never been saved - in that case
    ``Ctrl+S`` opens a dialog rather than writing a file, and a caller that did
    not say where to put it gets told so instead of a silent dialog.
    """
    live, problem = _live(engine)
    if problem:
        return problem
    if engine == "synthv":
        from . import synthv_live

        return synthv_live.state(_synthv_ipc())

    if not path:
        return {
            "ok": False,
            "engine": engine,
            "error": "no path given",
            "hint": "an unsaved project opens a save dialog; pass the path to write to",
        }
    win32.focus(live.hwnd, timeout=2.0)
    win32.hotkey("ctrl+shift+s")
    import time

    time.sleep(1.2)
    win32.type_text(path)
    win32.press([0x0D])
    time.sleep(1.5)
    return read_song(engine, path)


def read_song(engine: str, path: str) -> dict[str, Any]:
    """Parse a saved project with the engine's own reader."""
    from lhai.mcp.engines import cevio as cevio_engine

    readers = {"cevio": cevio_engine.read_ccs}
    if engine == "vocaloid":
        from lhai.mcp.engines import vocaloid as vocaloid_engine

        readers["vocaloid"] = vocaloid_engine.read_vsqx
    reader = readers.get(engine)
    if reader is None:
        return {"ok": False, "engine": engine, "error": f"no reader for {engine}"}
    try:
        song = reader(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "engine": engine, "path": path, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "engine": engine,
        "path": path,
        "name": song.name,
        "bpm": song.bpm,
        "tracks": [len(t.notes) for t in song.tracks],
        "song": song.to_dict() if hasattr(song, "to_dict") else None,
    }


def open_song(engine: str, path: str) -> dict[str, Any]:
    """Open a project file in the running editor (``Ctrl+O``)."""
    live, problem = _live(engine)
    if problem:
        return problem
    win32.focus(live.hwnd, timeout=2.0)
    win32.hotkey("ctrl+o")
    import time

    time.sleep(1.5)
    win32.type_text(path)
    win32.press([0x0D])
    time.sleep(2.0)
    return {"ok": True, "engine": engine, "path": path}


def _synthv_ipc() -> Path:
    from lhai.mcp import server

    return server.Store().ipc
