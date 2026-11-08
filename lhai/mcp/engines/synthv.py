"""Synthesizer V Studio adapter: ``.svp`` project files plus the Lua bridge.

The ``.svp`` schema is not published, so this module is built from a project
file the editor itself wrote (version 153, Synthesizer V Studio 2) and kept
field-for-field identical to it.  Inventing fields is the one thing that makes
SynthV reject a project, so the writer only emits keys that were observed in a
real file:

* ``time.tempo`` / ``time.meter`` (note: ``meter``, not ``timeSignature``),
* ``tracks[].mainGroup`` for the notes and ``tracks[].mainRef`` for the voice,
* ``mainRef.database`` for the singer, with ``backendType`` ``SVR2AI``,
* per-note ``systemAttributes`` + ``pitchTakes`` / ``timbreTakes``.

Two clocks matter: ``1 quarter note = 705600000`` blick, and every note offset
is absolute blick from the project start.

The live channel is the Lua script API.  Rather than inventing calls, the agent
script uses exactly the functions that are proven to work in this editor build
(``SV:getMainEditor``, ``track:getGroupReference``, ``SV:create("Note")``,
``note:setOnset``/``setDuration``/``setPitch``/``setLyrics``, ``group:addNote``,
``SV:setTimeout``, ``SV.finish``) and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from lhai.mcp.engines.base import find_engine
from lhai.mcp.ir import SNote, Song, Track, beat_to_tick, normalize_language

__all__ = [
    "BLICK_PER_QUARTER",
    "SVP_VERSION",
    "write_svp",
    "read_svp",
    "installed_voices",
    "scripts_dir",
    "install_bridge",
    "bridge_status",
    "push_to_synthv",
]

#: One quarter note in Synthesizer V ticks ("blick").
BLICK_PER_QUARTER = 705_600_000

#: Project schema version of the reference file (Synthesizer V Studio 2).
SVP_VERSION = 153

_PARAMETERS = (
    "pitchDelta",
    "vibratoEnv",
    "loudness",
    "tension",
    "breathiness",
    "voicing",
    "gender",
    "toneShift",
)

#: Phoneme set per language.  Only ``romaji`` is verified against a real file;
#: the rest are the names SynthV uses in its own UI.  A wrong phoneset is
#: ignored by the editor (it falls back to the voice's default), which is why
#: this map is allowed to be best-effort while the rest of the file is not.
_PHONESETS = {
    "japanese": "romaji",
    "english": "arpabet",
    "mandarin": "pinyin",
    "cantonese": "jyutping",
    "korean": "romaja",
    "spanish": "xsampa",
    "portuguese": "xsampa",
    "italian": "xsampa",
}


def _uuid() -> str:
    return str(uuid.uuid4())


def _param_curve() -> dict[str, Any]:
    return {"mode": "cubic", "points": []}


def _take() -> dict[str, Any]:
    return {"activeTakeId": 0, "takes": [{"id": 0, "expr": 0.0, "liked": False}]}


def _note_obj(note: SNote) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "musicalType": "singing",
        "onset": beat_to_tick(note.onset, BLICK_PER_QUARTER),
        "duration": max(1, beat_to_tick(note.duration, BLICK_PER_QUARTER)),
        "lyrics": note.lyric or "a",
        "phonemes": note.phonemes,
        "accent": "",
        "pitch": max(0, note.pitch),
        "detune": 0,
        "instantMode": True,
        "attributes": {"evenSyllableDuration": True},
        "systemAttributes": {
            "tF0Offset": 0.0,
            "tF0Left": 0.055,
            "tF0Right": 0.055,
            "dF0Left": 0.0,
            "dF0Right": 0.0,
            "dF0Vbr": 0.0,
            "evenSyllableDuration": True,
        },
        "pitchTakes": _take(),
        "timbreTakes": _take(),
    }
    return payload


def _point_curve(points: Iterable[Iterable[float]] | None) -> dict[str, Any]:
    curve = _param_curve()
    for point in points or []:
        values = list(point)
        if len(values) < 2:
            continue
        curve["points"].append(
            {"x": beat_to_tick(values[0], BLICK_PER_QUARTER), "y": float(values[1])}
        )
    return curve


def write_svp(song: Song, path: str | Path, *, sample_rate: int = 44100) -> Path:
    """Write ``song`` as a Synthesizer V Studio 2 project."""
    song.validate()
    path = Path(path)
    tracks: list[dict[str, Any]] = []

    for index, track in enumerate(song.tracks):
        group_id = _uuid()
        language = normalize_language(track.language)
        notes = [
            _note_obj(n) for n in track.sorted_notes() if n.pitch >= 0
        ]
        parameters = {name: _param_curve() for name in _PARAMETERS}
        for name, points in (track.controls or {}).items():
            if name in parameters:
                parameters[name] = _point_curve(points)
        tracks.append(
            {
                "name": track.name,
                "dispColor": "ff7db235",
                "dispOrder": index,
                "renderEnabled": True,
                "mixer": {
                    "gainDecibel": float(track.gain_db),
                    "pan": float(track.pan),
                    "mute": bool(track.muted),
                    "solo": bool(track.solo),
                    "display": True,
                },
                "mainGroup": {
                    "name": "main",
                    "uuid": group_id,
                    "parameters": parameters,
                    "vocalModes": {},
                    "notes": notes,
                },
                "mainRef": {
                    "groupID": group_id,
                    "blickAbsoluteBegin": 0,
                    "blickAbsoluteEnd": -1,
                    "blickOffset": 0,
                    "pitchOffset": 0,
                    "isInstrumental": False,
                    "systemPitchDelta": _param_curve(),
                    "database": {
                        "name": track.voice,
                        "language": language,
                        "phoneset": _PHONESETS.get(language, "romaji"),
                        "languageOverride": "",
                        "phonesetOverride": "",
                        "backendType": "SVR2AI",
                        "version": "200",
                    },
                    "dictionary": "",
                    "voice": {
                        "vocalModeInherited": True,
                        "vocalModePreset": "",
                        "vocalModeParams": {},
                    },
                    "pitchTakes": _take(),
                    "timbreTakes": _take(),
                },
                "groups": [],
            }
        )

    project = {
        "version": SVP_VERSION,
        "time": {
            "meter": [
                {
                    "index": 0,
                    "numerator": int(song.beats_per_bar),
                    "denominator": int(song.beat_unit),
                }
            ],
            "tempo": [{"position": 0, "bpm": float(song.bpm)}],
        },
        "library": [],
        "tracks": tracks,
        "renderConfig": {
            "destination": "",
            "filename": song.name,
            "numChannels": 1,
            "aspirationFormat": "noAspiration",
            "bitDepth": 16,
            "sampleRate": int(sample_rate),
            "exportMixDown": True,
            "exportPitch": False,
        },
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- #
# reader
# --------------------------------------------------------------------------- #
def read_svp(path: str | Path) -> Song:
    """Read a ``.svp`` written by Synthesizer V Studio 2.

    Tolerant on purpose: ``mainGroup`` is v2, ``notes`` at track level is the
    v1 layout, and files saved by other tools in between exist in the wild.  A
    field that is absent is treated as absent rather than fatal, because being
    able to import a colleague's project matters more than being strict about a
    schema nobody published.
    """
    path = Path(path)
    raw = path.read_bytes().rstrip(b"\x00 \t\r\n")
    data = json.loads(raw.decode("utf-8", "replace"))

    tempo = _first_bpm(data)
    meter = _first_meter(data)
    song = Song(name=path.stem, bpm=tempo or 120.0)
    if meter:
        song.beats_per_bar, song.beat_unit = meter
    song.meta.update(
        {"source": str(path), "source_format": "svp", "svp_version": data.get("version")}
    )

    for track_data in data.get("tracks") or []:
        group = track_data.get("mainGroup") or {}
        raw_notes = group.get("notes") or track_data.get("notes") or []
        notes = [_note_from_obj(n) for n in raw_notes]
        notes = [n for n in notes if n is not None]
        reference = track_data.get("mainRef") or {}
        database = reference.get("database") or {}
        controls: dict[str, list[list[float]]] = {}
        for name, curve in (group.get("parameters") or {}).items():
            points = [
                [p.get("x", 0) / BLICK_PER_QUARTER, p.get("y", 0.0)]
                for p in (curve or {}).get("points") or []
            ]
            if points:
                controls[name] = points
        mixer = track_data.get("mixer") or {}
        song.tracks.append(
            Track(
                name=track_data.get("name") or f"Track {len(song.tracks) + 1}",
                voice=database.get("name", "") or "",
                language=database.get("language", "") or "japanese",
                notes=notes,
                gain_db=float(mixer.get("gainDecibel", 0.0) or 0.0),
                pan=float(mixer.get("pan", 0.0) or 0.0),
                muted=bool(mixer.get("mute", False)),
                solo=bool(mixer.get("solo", False)),
                controls=controls,
            )
        )
    if not song.tracks:
        song.tracks.append(Track(name="Track 1"))
    return song


def _note_from_obj(obj: dict[str, Any]) -> SNote | None:
    try:
        onset = int(obj.get("onset", 0)) / BLICK_PER_QUARTER
        duration = int(obj.get("duration", 0)) / BLICK_PER_QUARTER
        pitch = int(obj.get("pitch", 60))
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return SNote(
        onset=round(onset, 6),
        duration=round(duration, 6),
        pitch=pitch,
        lyric=str(obj.get("lyrics") or obj.get("lyric") or "a"),
        phonemes=str(obj.get("phonemes") or ""),
    )


def _first_bpm(data: dict[str, Any]) -> float | None:
    for container in (data.get("time"), data.get("timeAxis")):
        for entry in (container or {}).get("tempo") or []:
            try:
                return float(entry.get("bpm", entry.get("value", 120)))
            except (TypeError, ValueError):
                continue
    return None


def _first_meter(data: dict[str, Any]) -> tuple[int, int] | None:
    for container in (data.get("time"), data.get("timeAxis")):
        container = container or {}
        for entry in container.get("meter") or container.get("timeSignature") or []:
            try:
                return int(entry.get("numerator", 4)), int(entry.get("denominator", 4))
            except (TypeError, ValueError):
                continue
    return None


# --------------------------------------------------------------------------- #
# voices / scripts
# --------------------------------------------------------------------------- #
def _appdata_root() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    root = Path(appdata) / "Dreamtonics"
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir() and "studio" in p.name.lower()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def installed_voices(
    folders: "Sequence[str | Path] | None" = None,
) -> list[dict[str, Any]]:
    """Singers usable in a project, discovered from real ``.svp`` files.

    Synthesizer V does **not** publish its installed-voice list anywhere
    readable: the voice data lives in ``databases/<uuid>/`` as encrypted
    ``.dnni`` blobs, and the readable catalog in ``databases/meta/`` is keyed by
    a different id space and lists every product on the store, not what is
    installed.  So the reliable source is a project the editor itself saved -
    each one records ``mainRef.database.name``.  Recent projects are read from
    ``settings.xml``, and any extra folder can be passed in.

    A name found this way is exactly the spelling to pass to ``track_set(voice=)``,
    which is the whole point.
    """
    found: dict[str, dict[str, Any]] = {}
    for path in _candidate_projects(folders):
        try:
            raw = path.read_bytes().rstrip(b"\x00 \t\r\n")
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            continue
        for track in data.get("tracks") or []:
            database = (track.get("mainRef") or {}).get("database") or {}
            name = str(database.get("name") or "").strip()
            if not name:
                continue
            entry = found.setdefault(
                name,
                {
                    "name": name,
                    "language": database.get("language", ""),
                    "phoneset": database.get("phoneset", ""),
                    "backend": database.get("backendType", ""),
                    "source": "project",
                    "seen_in": [],
                },
            )
            if str(path) not in entry["seen_in"]:
                entry["seen_in"].append(str(path))
            if len(entry["seen_in"]) > 3:
                entry["seen_in"] = entry["seen_in"][:3]

    if found:
        return sorted(found.values(), key=lambda v: v["name"].lower())
    return _catalog_voices()


def _candidate_projects(folders: "Sequence[str | Path] | None") -> list[Path]:
    """Project files worth reading for voice names.

    Recent files are used as *hints to folders*: a user's recent list names one
    file, but the singers they own are visible in every project beside it.  The
    cap keeps a folder of 500 projects from turning a voice query into a
    multi-second scan.
    """
    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path).lower()
        if key not in seen and path.suffix.lower() == ".svp":
            seen.add(key)
            paths.append(path)

    for folder in folders or ():
        base = Path(folder)
        if base.is_file():
            add(base)
        elif base.is_dir():
            for entry in sorted(base.glob("*.svp"))[:60]:
                add(entry)

    for recent in _recent_project_paths():
        if recent.exists():
            add(recent)
        parent = recent.parent
        if parent.exists():
            try:
                for entry in sorted(parent.glob("*.svp"))[:15]:
                    add(entry)
            except OSError:
                continue
    return paths[:80]


def _recent_project_paths() -> list[Path]:
    root = _appdata_root()
    if root is None:
        return []
    settings = root / "settings" / "settings.xml"
    if not settings.exists():
        return []
    try:
        text = settings.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Path] = []
    for match in re.finditer(r'<FileItem[^>]*path="([^"]+)"', text):
        raw = match.group(1)
        path = Path(_unescape_xml(raw))
        if path.suffix.lower() == ".svp" and path not in out:
            out.append(path)
    return out[:40]


def _unescape_xml(text: str) -> str:
    import html

    return html.unescape(text)


def _catalog_voices() -> list[dict[str, Any]]:
    """Store catalog - every product, not the installed subset.

    Used only when no project file could be read, because a valid spelling plus
    an honest ``source: "catalog"`` marker beats an empty list.
    """
    root = _appdata_root()
    if root is None:
        return []
    meta = root / "databases" / "meta"
    if not meta.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(meta.glob("*.json")):
        try:
            info = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(info.get("name") or "").strip()
        if not name:
            continue
        out.append(
            {
                "name": name,
                "vendor": info.get("vendor", ""),
                "languages": info.get("languages", []),
                "pitch_range": info.get("pitchRange", ""),
                "source": "catalog",
            }
        )
    return out


def scripts_dir() -> Path | None:
    from lhai.live import synthv_lua

    return synthv_lua.scripts_dir()


BRIDGE_NAME = "LHai_Agent.lua"


def install_bridge(ipc_dir: str | Path) -> dict[str, Any]:
    """Install (or refresh) the resident Lua agent into SynthV's script folder.

    The script is generated rather than copied so the IPC directory is baked in:
    SynthV's Lua environment has no reliable way to read an environment
    variable, and a script that cannot find its command file is a silent no-op.

    The source lives in :mod:`lhai.live.synthv_lua` (protocol v3) - the same file
    the live tools use, so there is only one bridge to keep correct.
    """
    from lhai.live import synthv_lua

    result = dict(synthv_lua.install(ipc_dir))
    result.setdefault(
        "next",
        "In Synthesizer V: Scripts > LHai Agent. Run it once per session; "
        "it stays resident and polls for commands.",
    )
    return result


def bridge_status(ipc_dir: str | Path) -> dict[str, Any]:
    from lhai.live import synthv_lua

    status = dict(synthv_lua.status(ipc_dir))
    status["engine_installed"] = find_engine("synthv").installed
    return status


# --------------------------------------------------------------------------- #
# live push
# --------------------------------------------------------------------------- #
def push_to_synthv(
    song: Song,
    *,
    track: int = 0,
    ipc_dir: str | Path,
    clear: bool = True,
    group: int = 1,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Send notes to the running SynthV instance through the Lua agent.

    The transport (and the timeout diagnosis) lives in
    :func:`lhai.live.synthv_live.push_notes`; this wrapper only turns a
    :class:`Song` into the note dicts the bridge speaks.
    """
    from lhai.live import synthv_live

    if not song.tracks:
        return {"pushed": False, "reason": "song has no tracks"}
    if not 0 <= track < len(song.tracks):
        return {"pushed": False, "reason": f"track index {track} out of range"}
    track_obj = song.tracks[track]
    notes = [
        {
            "onset": note.onset,
            "duration": note.duration,
            "pitch": note.pitch,
            "lyric": note.lyric,
            "language": note.language or track_obj.language,
        }
        for note in track_obj.sorted_notes()
        if note.pitch >= 0
    ]
    result = synthv_live.push_notes(ipc_dir, notes, group=group, clear=clear, timeout=timeout)
    result["pushed"] = bool(result.get("ok"))
    result["track"] = track_obj.name
    result["notes"] = len(notes)
    if not result["pushed"]:
        result.setdefault("reason", result.get("error", "push failed"))
    return result
