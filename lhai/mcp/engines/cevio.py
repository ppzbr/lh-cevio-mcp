"""CeVIO AI adapter: ``.ccs`` scenario XML, plus MIDI import as the fallback.

CeVIO AI has no scripting interface at all, so the only channel is the project
file.  A ``.ccs`` is a small XML document (``Scenario`` -> ``Sequence`` ->
``Scene`` -> ``Groups`` + ``Units``) whose clock is not documented either; the
constants below come from a real project file:

* 1 quarter note = 960 clock,
* bar 1 starts at clock 3840, i.e. one 4/4 measure of pre-roll that is always
  present in the score,
* pitch is split into ``PitchStep`` (0..11) and ``PitchOctave`` where
  ``C4 = step 0, octave 4`` (so MIDI 60).

A ``.ccs`` that CeVIO refuses to open is worse than no file, so MIDI export is
offered as a first-class path (``write_midi`` in :mod:`lhai.mcp.midiio`) and the
MCP tool reports both.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from lhai.mcp.engines.base import find_engine
from lhai.mcp.ir import SNote, Song, Track, normalize_language

__all__ = [
    "CCS_CLOCKS_PER_QUARTER",
    "CCS_PRE_MEASURE_CLOCK",
    "write_ccs",
    "read_ccs",
    "installed_voices",
]

#: CeVIO clock units per quarter note.
CCS_CLOCKS_PER_QUARTER = 960

#: Clock of bar 1 - one 4/4 measure of pre-roll that every real project has.
CCS_PRE_MEASURE_CLOCK = 3840

_CCS_LANGUAGES = {
    "japanese": "Japanese",
    "mandarin": "Chinese",
    "cantonese": "Chinese",
    "english": "English",
    "korean": "Korean",
    "spanish": "Spanish",
}


def _guid() -> str:
    return str(uuid.uuid4())


def _clock(beats: float) -> int:
    return int(round(float(beats) * CCS_CLOCKS_PER_QUARTER)) + CCS_PRE_MEASURE_CLOCK


def _hms(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def write_ccs(song: Song, path: str | Path) -> Path:
    """Write ``song`` as a CeVIO AI ``.ccs`` scenario."""
    song.validate()
    path = Path(path)
    tracks = song.tracks or [Track(name="Track 1")]
    duration = _hms(song.seconds() + 2.0)

    groups: list[tuple[str, Track]] = [(f"{{{_guid()}}}", t) for t in tracks]

    out: list[str] = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        "<Scenario>",
        '  <Generation Author="lh-ai-studio" Version="3.0.0.0" />',
        "  <Sequence>",
        "    <Scene>",
        "      <Groups>",
    ]
    for group_id, track in groups:
        out.append(
            f'        <Group Version="1.0" Id="{group_id}" Category="SingerSong"'
            f' Name="{_attr(track.name)}" Color="#FF7DB235"'
            f' Volume="{_int_attr(track.gain_db)}" Pan="{_int_attr(track.pan * 100)}"'
            f' IsSolo="{_bool(track.solo)}" IsMuted="{_bool(track.muted)}" />'
        )
    out.append("      </Groups>")
    out.append("      <Units>")

    for group_id, track in groups:
        language = _CCS_LANGUAGES.get(normalize_language(track.language), "Japanese")
        unit_id = f"{{{_guid()}}}"
        out.extend(
            [
                f'        <Unit Version="1.0" Id="{unit_id}" Category="SingerSong"'
                f' Group="{group_id}" StartTime="00:00:00" Duration="{duration}"'
                f' Language="{language}">',
                '          <Song Version="1.02">',
                f'            <Tempo><Sound Clock="0" Tempo="{_num(song.bpm)}" /></Tempo>',
                f'            <Beat><Time Clock="0" Beats="{int(song.beats_per_bar)}"'
                f' BeatType="{int(song.beat_unit)}" /></Beat>',
                "            <Score>",
                '              <Key Clock="0" Fifths="0" Mode="0" />',
            ]
        )

        previous_clock = CCS_PRE_MEASURE_CLOCK
        previous_duration = 0
        for note in track.sorted_notes():
            if note.pitch < 0:
                continue
            clock = _clock(note.onset)
            duration_clock = max(1, int(round(note.duration * CCS_CLOCKS_PER_QUARTER)))
            # A CeVIO note carries its *duration*, but the previous note's
            # duration is what the editor uses to keep the timeline contiguous;
            # writing both keeps the score gapless when rounding bites.
            if clock < previous_clock + previous_duration:
                clock = previous_clock + previous_duration
            step = note.pitch % 12
            octave = note.pitch // 12 - 1
            out.append(
                f'              <Note Clock="{clock}" Duration="{duration_clock}"'
                f' PitchStep="{step}" PitchOctave="{octave}"'
                f' Lyric="{_attr(note.lyric or "a")}" />'
            )
            previous_clock, previous_duration = clock, duration_clock

        out.extend(["            </Score>", "          </Song>", "        </Unit>"])

    out.extend(["      </Units>", "    </Scene>", "  </Sequence>", "</Scenario>"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _attr(text: Any) -> str:
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _int_attr(value: Any) -> str:
    try:
        return str(int(round(float(value))))
    except (TypeError, ValueError):
        return "0"


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "120"
    return f"{number:g}"


# --------------------------------------------------------------------------- #
# reader
# --------------------------------------------------------------------------- #
_NS = re.compile(r'(xmlns(:\w+)?|\w+:\w+)="[^"]*"')
_TAG = re.compile(r"<(/?)\w+:")


def read_ccs(path: str | Path) -> Song:
    """Read a CeVIO AI ``.ccs`` scenario."""
    path = Path(path)
    text = _TAG.sub(r"<\1", _NS.sub("", path.read_text(encoding="utf-8", errors="replace")))
    root = ET.fromstring(text)
    song = Song(name=path.stem)
    song.meta.update({"source": str(path), "source_format": "ccs"})

    tempo = root.find(".//Song/Tempo/Sound")
    if tempo is not None:
        song.bpm = _float(tempo.get("Tempo"), 120.0)
    beat = root.find(".//Song/Beat/Time")
    if beat is not None:
        song.beats_per_bar = int(_float(beat.get("Beats"), 4) or 4)
        song.beat_unit = int(_float(beat.get("BeatType"), 4) or 4)

    names: dict[str, str] = {}
    for group in root.findall(".//Groups/Group"):
        group_id = group.get("Id") or ""
        names[group_id.strip("{}")] = group.get("Name") or "Track"

    for unit in root.findall(".//Units/Unit"):
        group_id = (unit.get("Group") or "").strip("{}")
        name = names.get(group_id) or unit.get("Name") or f"Track {len(song.tracks) + 1}"
        language = (unit.get("Language") or "Japanese").lower()
        notes: list[SNote] = []
        for note in unit.findall(".//Score/Note"):
            clock = int(_float(note.get("Clock"), 0))
            duration_clock = int(_float(note.get("Duration"), 480))
            if duration_clock <= 0:
                continue
            step = int(_float(note.get("PitchStep"), 0))
            octave = int(_float(note.get("PitchOctave"), 4))
            notes.append(
                SNote(
                    onset=round((clock - CCS_PRE_MEASURE_CLOCK) / CCS_CLOCKS_PER_QUARTER, 6),
                    duration=round(duration_clock / CCS_CLOCKS_PER_QUARTER, 6),
                    pitch=(octave + 1) * 12 + step,
                    lyric=note.get("Lyric") or "a",
                )
            )
        if notes:
            notes.sort(key=lambda n: n.onset)
            song.tracks.append(Track(name=name, language=language, notes=notes))
    if not song.tracks:
        song.tracks.append(Track(name="Track 1"))
    return song


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# voices
# --------------------------------------------------------------------------- #
def installed_voices() -> list[dict[str, Any]]:
    """Song voices from CeVIO's own ``VocalSource`` registry.

    Each installed voice is a folder under ``Configuration/VocalSource/Song``
    holding a ``setting.cfg`` (XML) whose ``<VocalSource Name=...>`` *is* the
    display name the editor shows - the folder name is only a code (``CSNV-JPF-
    FP5`` is "IA").  ``Components`` are the voice's styles.
    """
    install = find_engine("cevio")
    if install.root is None:
        return []
    out: list[dict[str, Any]] = []
    for folder in _vocal_source_dirs(install.root):
        setting = folder / "setting.cfg"
        if setting.exists():
            try:
                root = ET.fromstring(setting.read_text(encoding="utf-8", errors="replace"))
            except ET.ParseError:
                root = None
            if root is not None:
                styles = [
                    str(c.get("Name") or c.get("Title") or "")
                    for c in root.findall("./ComponentSettings/Component")
                ]
                out.append(
                    {
                        "name": str(root.get("Name") or folder.name),
                        "id": str(root.get("Id") or ""),
                        "path": str(folder),
                        "styles": [s for s in styles if s],
                    }
                )
                continue
        # Not a package (or an unreadable one): keep the folder, say so.
        out.append({"name": folder.name, "path": str(folder)})
    return out


def _vocal_source_dirs(root: Path) -> list[Path]:
    """Song-voice folders for both install layouts, plus the loose ``*Voice*``
    folders older CeVIO versions used."""
    candidates = [root / "Configuration" / "VocalSource" / "Song", root.parent]
    found: list[Path] = []
    for base in candidates:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            if entry.parent.name == "Song" or "voice" in entry.name.lower():
                if entry not in found:
                    found.append(entry)
    return found
