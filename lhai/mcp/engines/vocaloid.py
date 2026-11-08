"""VOCALOID adapter: ``.vsqx`` (vsq4 XML) plus MIDI, and nothing else.

VOCALOID has no external API - the editor is a closed application - so the only
two honest channels are "write a file the editor opens" and "let the user open
it".  Both are implemented here; neither pretends to be live control.

The ``.vsqx`` writer follows ``vsq4.xsd``, which ships inside the editor
(``Editor/vsq4.xsd``) and is therefore authoritative rather than remembered:

* element order is fixed by the schema (``vender``, ``version``,
  ``vVoiceTable``, ``mixer``, ``masterTrack``, ``vsTrack``+, ``monoTrack``,
  ``stTrack``) and a file with the wrong order is rejected outright,
* ``note`` is ``t``/``dur``/``n``/``v``/``y``/``p`` in that order, with the
  lyric in ``y`` and the phonemes in ``p``,
* positions are 0-based with ``preMeasure`` 0, which is schema-legal and avoids
  the pre-measure ambiguity that shifts every note by one bar when guessed
  wrong,
* ``vVoice.id`` must be exactly 16 characters (``typeComponentID``).

MIDI import is the fallback for the one thing a hand-written ``.vsqx`` cannot
guarantee: the voicebank component id.  A wrong id makes the editor fall back
to its default singer, which is graceful, but a user who cares passes
``voice_id`` (obtainable from :func:`voice_ids_in_folder`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from lhai.mcp.engines.base import find_engine
from lhai.mcp.ir import Song, Track, beat_to_tick

__all__ = [
    "VSQX_RESOLUTION",
    "write_vsqx",
    "read_vsqx",
    "installed_voices",
    "voice_ids_in_folder",
]

VSQX_RESOLUTION = 480

#: Schema version string, max 15 chars (``typeStrVersion``).
_VSQX_VERSION = "4.0.0.3"

#: Placeholder component id: 16 characters, which is what ``typeComponentID``
#: demands.  VOCALOID falls back to the default voice rather than refusing the
#: file, so a placeholder is safe and documented, not a bug.
DEFAULT_VOICE_ID = "0000000000000000"

_MIXER_DEFAULTS = {"sLvl": -898, "pan": 64, "vol": 0}


def _cdata(text: Any) -> str:
    value = str(text if text is not None else "")
    return "<![CDATA[" + value.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, low: int, high: int, default: int = 0) -> int:
    return max(low, min(high, _int(value, default)))


def write_vsqx(
    song: Song,
    path: str | Path,
    *,
    voice_id: str = DEFAULT_VOICE_ID,
    voice_name: str = "",
    vendor: str = "Yamaha Corporation",
) -> Path:
    """Write ``song`` as a ``.vsqx`` that validates against ``vsq4.xsd``."""
    song.validate()
    path = Path(path)
    tracks = song.tracks or [Track(name="Track 1")]
    resolution = VSQX_RESOLUTION
    ids = _fit_component_id(voice_id)

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        '<vsq4 xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/">',
        f"  <vender>{_cdata(vendor)}</vender>",
        f"  <version>{_cdata(_VSQX_VERSION)}</version>",
        "  <vVoiceTable>",
    ]

    for track in tracks:
        lines.extend(
            [
                "    <vVoice>",
                "      <bs>0</bs>",
                "      <pc>0</pc>",
                f"      <id>{_cdata(ids)}</id>",
                f"      <name>{_cdata(voice_name or track.voice or 'VOCALOID')}</name>",
                "      <vPrm>",
                "        <bre>0</bre>",
                "        <bri>0</bri>",
                "        <cle>0</cle>",
                "        <gen>0</gen>",
                "        <ope>0</ope>",
                "      </vPrm>",
                "    </vVoice>",
            ]
        )
    lines.append("  </vVoiceTable>")

    # -- mixer ------------------------------------------------------------- #
    lines.extend(
        [
            "  <mixer>",
            "    <masterUnit>",
            "      <oDev>0</oDev>",
            "      <rLvl>0</rLvl>",
            "      <vol>0</vol>",
            "    </masterUnit>",
        ]
    )
    for index, track in enumerate(tracks):
        lines.extend(
            [
                "    <vsUnit>",
                f"      <tNo>{index}</tNo>",
                "      <iGin>0</iGin>",
                f"      <sLvl>{_clamp(_MIXER_DEFAULTS['sLvl'], -898, 0, -898)}</sLvl>",
                "      <sEnable>1</sEnable>",
                f"      <m>{1 if track.muted else 0}</m>",
                f"      <s>{1 if track.solo else 0}</s>",
                f"      <pan>{_pan_value(track.pan)}</pan>",
                f"      <vol>{_clamp(track.gain_db * 100, -898, 0, 0)}</vol>",
                "    </vsUnit>",
            ]
        )
    lines.extend(
        [
            "    <monoUnit>",
            "      <iGin>0</iGin>",
            f"      <sLvl>{_MIXER_DEFAULTS['sLvl']}</sLvl>",
            "      <sEnable>0</sEnable>",
            "      <m>0</m>",
            "      <s>0</s>",
            f"      <pan>{_MIXER_DEFAULTS['pan']}</pan>",
            "      <vol>0</vol>",
            "    </monoUnit>",
            "    <stUnit>",
            "      <iGin>0</iGin>",
            "      <m>0</m>",
            "      <s>0</s>",
            "      <vol>0</vol>",
            "    </stUnit>",
            "  </mixer>",
        ]
    )

    # -- master track ------------------------------------------------------ #
    lines.extend(
        [
            "  <masterTrack>",
            f"    <seqName>{_cdata(song.name)}</seqName>",
            f"    <comment>{_cdata(song.meta.get('comment', ''))}</comment>",
            f"    <resolution>{resolution}</resolution>",
            "    <preMeasure>0</preMeasure>",
            "    <timeSig>",
            "      <m>0</m>",
            f"      <nu>{int(song.beats_per_bar)}</nu>",
            f"      <de>{int(song.beat_unit)}</de>",
            "    </timeSig>",
            "    <tempo>",
            "      <t>0</t>",
            f"      <v>{_int(float(song.bpm) * 100)}</v>",
            "    </tempo>",
            "  </masterTrack>",
        ]
    )

    # -- vocal tracks ------------------------------------------------------ #
    for index, track in enumerate(tracks):
        notes = [n for n in track.sorted_notes() if n.pitch >= 0]
        start = beat_to_tick(notes[0].onset, resolution) if notes else 0
        end = max((beat_to_tick(n.end, resolution) for n in notes), default=start)
        lines.extend(
            [
                "  <vsTrack>",
                f"    <tNo>{index}</tNo>",
                f"    <name>{_cdata(track.name)}</name>",
                f"    <comment>{_cdata('')}</comment>",
                "    <vsPart>",
                f"      <t>{start}</t>",
                f"      <playTime>{max(1, end - start)}</playTime>",
                f"      <name>{_cdata(track.name)}</name>",
                f"      <comment>{_cdata('')}</comment>",
                "      <sPlug>",
                f"        <id>{_cdata('00000000-0000-0000-0000-000000000000')}</id>",
                f"        <name>{_cdata('VOCALOID')}</name>",
                f"        <version>{_cdata('6.0.0')}</version>",
                "      </sPlug>",
                "      <singer>",
                f"        <t>{start}</t>",
                "        <bs>0</bs>",
                "        <pc>0</pc>",
                "      </singer>",
            ]
        )
        for note in notes:
            position = beat_to_tick(note.onset, resolution)
            duration = max(1, beat_to_tick(note.duration, resolution))
            lines.extend(
                [
                    "      <note>",
                    f"        <t>{position}</t>",
                    f"        <dur>{duration}</dur>",
                    f"        <n>{_clamp(note.pitch, 0, 127, 60)}</n>",
                    "        <v>64</v>",
                    f"        <y>{_cdata(note.lyric or 'a')}</y>",
                    f"        <p>{_cdata(note.phonemes)}</p>",
                    "      </note>",
                ]
            )
        lines.extend(["    </vsPart>", "  </vsTrack>"])

    lines.extend(["  <monoTrack />", "  <stTrack />", "</vsq4>"])

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fit_component_id(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z]", "", str(value or ""))
    return (cleaned + "0" * 16)[:16]


def _pan_value(pan: float) -> int:
    return _clamp(64 + float(pan) * 64.0, 0, 127, 64)


# --------------------------------------------------------------------------- #
# reader
# --------------------------------------------------------------------------- #
_NS = re.compile(r'(xmlns(:\w+)?|\w+:\w+)="[^"]*"')
_TAG = re.compile(r"<(/?)\w+:")


def _local(xml_text: str) -> str:
    return _TAG.sub(r"<\1", _NS.sub("", xml_text))


def read_vsqx(path: str | Path) -> Song:
    """Read a ``.vsqx`` (any vsq4 revision, and the vsq3 fallback names)."""
    path = Path(path)
    root = ET.fromstring(_local(path.read_text(encoding="utf-8", errors="replace")))
    song = Song(name=path.stem)
    song.meta.update({"source": str(path), "source_format": "vsqx"})

    master = root.find("masterTrack")
    resolution = VSQX_RESOLUTION
    if master is not None:
        resolution = _int(master.findtext("resolution"), VSQX_RESOLUTION) or VSQX_RESOLUTION
        name = master.findtext("seqName")
        if name:
            song.name = name
        tempo = master.find("tempo")
        if tempo is not None:
            raw = _int(tempo.findtext("v"), 12000)
            song.bpm = round(raw / 100.0, 4) if raw > 1000 else float(raw or 120)
        meter = master.find("timeSig")
        if meter is not None:
            song.beats_per_bar = _int(meter.findtext("nu"), 4) or 4
            song.beat_unit = _int(meter.findtext("de"), 4) or 4

    voice_names: dict[int, str] = {}
    for index, voice in enumerate(root.findall(".//vVoiceTable/vVoice")):
        voice_names[index] = voice.findtext("name") or ""
        voice_id = voice.findtext("id")
        if voice_id:
            song.meta.setdefault("vocaloid_voice_ids", []).append(voice_id)

    for index, element in enumerate(root.findall("vsTrack")):
        name = element.findtext("name") or f"Track {index + 1}"
        notes = []
        for note in element.findall(".//note"):
            position = _int(note.findtext("t"), 0)
            duration = _int(note.findtext("dur"), resolution)
            if duration <= 0:
                continue
            if note.find("posTick") is not None:  # vsq3 naming
                position = _int(note.findtext("posTick"), 0)
                duration = _int(note.findtext("durTick"), resolution)
            notes.append(
                {
                    "onset": round(position / resolution, 6),
                    "duration": round(duration / resolution, 6),
                    "pitch": _clamp(note.findtext("n", note.findtext("noteNum", "60")), 0, 127, 60),
                    "lyric": note.findtext("y") or note.findtext("lyric") or "a",
                    "phonemes": note.findtext("p") or "",
                }
            )
        song.tracks.append(
            Track(
                name=name,
                voice=voice_names.get(index, "") or "",
                notes=[_note(row) for row in notes],
            )
        )
    if not song.tracks:
        song.tracks.append(Track(name="Track 1"))
    return song


def _note(row: dict[str, Any]):
    from lhai.mcp.ir import SNote

    return SNote(**row)


# --------------------------------------------------------------------------- #
# voices
# --------------------------------------------------------------------------- #
def installed_voices(root: str | Path | None = None) -> list[dict[str, Any]]:
    """Voicebank folders on disk.  Names only - ids live inside encrypted data."""
    install = find_engine("vocaloid")
    base = Path(root) if root else None
    if base is None and install.root:
        base = install.root.parent / "CommonFiles" / "Model"
    out: list[dict[str, Any]] = []
    if base is None or not base.exists():
        return out
    for folder in sorted(base.iterdir()):
        if not folder.is_dir():
            continue
        styles = sorted({p.stem.split("_", 2)[-1] for p in folder.glob("*.vtb2")})
        out.append({"name": folder.name, "path": str(folder), "styles": styles})
    return out


def voice_ids_in_folder(folder: str | Path, *, limit: int = 200) -> list[str]:
    """Collect 16-char component ids from existing ``.vsqx``/``.vsq`` files.

    This is how a user gets a real ``vVoice.id`` without a hex editor: open one
    project in VOCALOID, save it, point this at the folder, and the ids the
    editor itself wrote come back.
    """
    found: list[str] = []
    base = Path(folder)
    if not base.exists():
        return found
    pattern = re.compile(r"<id>\s*(?:<!\[CDATA\[)?([0-9A-Za-z]{16})(?:\]\]>)?\s*</id>")
    for pattern_file in list(base.rglob("*.vsqx"))[:limit] + list(base.rglob("*.vsq"))[:limit]:
        try:
            text = pattern_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in pattern.findall(text):
            if match not in found:
                found.append(match)
    return found


def language_note() -> str:
    return (
        "vsqx has no language field; VOCALOID picks the lyrics' language from the "
        "voicebank. Keep lyrics in the singer's own script (kana/romaji/pinyin/English)."
    )
