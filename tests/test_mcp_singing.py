"""Tests for the singing-engine MCP: IR, adapters and tool surface.

Everything here runs offline and without any of the four applications: the
writers are checked against the schema facts they were built from (vsq4 XSD
element order, Synthesizer V's 705600000-blick quarter, CeVIO's 3840-clock
pre-measure), and every format is round-tripped through its own reader.
"""

from __future__ import annotations

import asyncio
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from lhai.mcp import SNote, Song, Store, Track, split_lyrics
from lhai.mcp.engines import cevio, export_song, import_song, synthv, vocaloid
from lhai.mcp.ir import normalize_language
from lhai.mcp.midiio import read_midi, write_midi
from lhai.mcp.server import build_server


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
def make_song() -> Song:
    return Song(
        name="test song",
        bpm=128.0,
        beats_per_bar=4,
        beat_unit=4,
        tracks=[
            Track(
                name="Lead",
                voice="Kasane Teto",
                language="japanese",
                notes=[
                    SNote(onset=0.0, duration=0.5, pitch=60, lyric="ka"),
                    SNote(onset=0.5, duration=0.5, pitch=62, lyric="ze"),
                    SNote(onset=1.0, duration=1.0, pitch=64, lyric="ni"),
                    SNote(onset=2.0, duration=0.75, pitch=65, lyric="no"),
                    SNote(onset=2.75, duration=1.25, pitch=67, lyric="ru"),
                ],
            ),
            Track(
                name="Harmony",
                voice="Yi Xi 2",
                language="mandarin",
                notes=[SNote(onset=1.0, duration=2.0, pitch=52, lyric="啊")],
            ),
        ],
    )


def rows(song: Song, index: int = 0) -> list[tuple[float, float, int, str]]:
    return [
        (round(n.onset, 6), round(n.duration, 6), n.pitch, n.lyric)
        for n in song.tracks[index].sorted_notes()
    ]


# --------------------------------------------------------------------------- #
# IR
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ka-ze-ni-no-ru", ["ka", "ze", "ni", "no", "ru"]),
        ("君の声", ["君", "の", "声"]),
        ("hello world", ["hello", "world"]),
        ("kanade", ["ka", "na", "de"]),
        ("さくら", ["さくら"]),
        ("a|b c", ["a", "b", "c"]),
        ("", []),
    ],
)
def test_split_lyrics(text: str, expected: list[str]) -> None:
    assert split_lyrics(text) == expected


def test_split_lyrics_syllable_mode() -> None:
    assert split_lyrics("hello world", latin="syllables") == ["he", "llo", "wo", "rld"]


def test_language_aliases() -> None:
    assert normalize_language("zh-CN") == "mandarin"
    assert normalize_language("EN") == "english"
    assert normalize_language("日本語") == "japanese"
    assert normalize_language("") == "japanese"
    assert normalize_language("klingon", default="english") == "english"


def test_song_validation_and_overlap() -> None:
    song = make_song()
    assert song.validate(strict=False) == []
    assert song.overlaps() == []
    song.tracks[0].notes.append(SNote(onset=0.25, duration=1.0, pitch=72, lyric="a"))
    assert song.overlaps()


def test_piano_roll_renders() -> None:
    text = make_song().piano_roll(0)
    assert "[=====" in text.replace("=", "=") or "[" in text
    assert "5 notes" in text


def test_store_roundtrip(tmp_path: Path) -> None:
    store = Store(home=tmp_path)
    song = make_song()
    path = store.save(song)
    assert path.exists()
    back = store.load("test song")
    assert back.bpm == song.bpm
    assert rows(back, 0) == rows(song, 0)
    assert store.list()[0]["notes"] == 6
    assert store.delete("test song") is True
    assert store.list() == []


# --------------------------------------------------------------------------- #
# Synthesizer V
# --------------------------------------------------------------------------- #
def test_svp_blick_and_schema(tmp_path: Path) -> None:
    path = synthv.write_svp(make_song(), tmp_path / "song.svp")
    data = json.loads(path.read_text(encoding="utf-8"))

    # keys observed in a project the editor itself saved (version 153)
    assert list(data) == ["version", "time", "library", "tracks", "renderConfig"]
    assert data["version"] == 153
    assert data["time"]["tempo"][0]["bpm"] == 128.0
    assert data["time"]["meter"][0]["numerator"] == 4

    track = data["tracks"][0]
    assert set(track) == {
        "name", "dispColor", "dispOrder", "renderEnabled",
        "mixer", "mainGroup", "mainRef", "groups",
    }
    assert track["mainGroup"]["uuid"] == track["mainRef"]["groupID"]
    assert track["mainRef"]["database"]["name"] == "Kasane Teto"
    assert track["mainRef"]["database"]["backendType"] == "SVR2AI"

    note = track["mainGroup"]["notes"][1]
    assert note["onset"] == 352_800_000  # half a quarter
    assert note["duration"] == 352_800_000
    assert note["pitch"] == 62
    assert note["lyrics"] == "ze"
    for key in ("musicalType", "instantMode", "attributes", "systemAttributes",
                "pitchTakes", "timbreTakes"):
        assert key in note


def test_svp_roundtrip(tmp_path: Path) -> None:
    song = make_song()
    path = synthv.write_svp(song, tmp_path / "song.svp")
    back = synthv.read_svp(path)
    assert back.bpm == 128.0
    assert len(back.tracks) == 2
    assert rows(back, 0) == rows(song, 0)
    assert rows(back, 1) == rows(song, 1)
    assert back.tracks[0].voice == "Kasane Teto"
    assert back.tracks[1].language == "mandarin"


def test_svp_reads_legacy_shape(tmp_path: Path) -> None:
    legacy = {
        "version": 113,
        "timeAxis": {
            "tempo": [{"position": 0, "bpm": 90}],
            "timeSignature": [{"position": 0, "numerator": 3, "denominator": 4}],
        },
        "tracks": [
            {
                "name": "Old",
                "notes": [
                    {"onset": 0, "duration": 705600000, "pitch": 60, "lyrics": "la"}
                ],
            }
        ],
    }
    path = tmp_path / "old.svp"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    song = synthv.read_svp(path)
    assert song.bpm == 90
    assert (song.beats_per_bar, song.beat_unit) == (3, 4)
    assert rows(song, 0) == [(0.0, 1.0, 60, "la")]


# --------------------------------------------------------------------------- #
# VOCALOID
# --------------------------------------------------------------------------- #
def test_vsqx_matches_schema_order(tmp_path: Path) -> None:
    path = vocaloid.write_vsqx(make_song(), tmp_path / "song.vsqx")
    text = path.read_text(encoding="utf-8")
    # vsq4.xsd declares elementFormDefault="qualified", so instances must carry
    # the namespace; strip it here so the assertions can use plain tag names.
    ns = 'xmlns="http://www.yamaha.co.jp/vocaloid/schema/vsq4/"'
    assert ns in text
    root = ET.fromstring(text.replace(ns, "", 1))
    order = [child.tag for child in root]
    assert order == [
        "vender", "version", "vVoiceTable", "mixer", "masterTrack",
        "vsTrack", "vsTrack", "monoTrack", "stTrack",
    ]
    # mixer children, in schema order
    assert [c.tag for c in root.find("mixer")] == [
        "masterUnit", "vsUnit", "vsUnit", "monoUnit", "stUnit",
    ]
    # note children, in schema order
    note = root.findall(".//note")[0]
    assert [c.tag for c in note] == ["t", "dur", "n", "v", "y", "p"]
    # component id must be exactly 16 characters (typeComponentID)
    assert len(root.findtext(".//vVoice/id")) == 16
    # tempo is centi-bpm
    assert root.findtext("masterTrack/tempo/v") == "12800"


def test_vsqx_roundtrip(tmp_path: Path) -> None:
    song = make_song()
    path = vocaloid.write_vsqx(song, tmp_path / "song.vsqx")
    back = vocaloid.read_vsqx(path)
    assert back.bpm == 128.0
    assert rows(back, 0) == rows(song, 0)
    assert rows(back, 1) == rows(song, 1)
    assert back.name == "test song"
    assert back.tracks[0].voice == "Kasane Teto"


# --------------------------------------------------------------------------- #
# CeVIO
# --------------------------------------------------------------------------- #
def test_ccs_clock_and_structure(tmp_path: Path) -> None:
    path = cevio.write_ccs(make_song(), tmp_path / "song.ccs")
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    assert root.tag == "Scenario"
    assert root.find("Sequence/Scene/Groups") is not None
    assert len(root.findall(".//Groups/Group")) == 2

    unit = root.find(".//Units/Unit")
    assert unit is not None
    assert unit.get("Language") == "Japanese"
    sound = root.find(".//Song/Tempo/Sound")
    assert sound is not None and sound.get("Tempo") == "128"

    notes = root.findall(".//Score/Note")
    # bar 1 starts at clock 3840; one beat is 960 clock
    assert notes[0].get("Clock") == "3840"
    assert notes[1].get("Clock") == "4320"
    assert notes[0].get("Duration") == "480"
    # MIDI 60 -> step 0, octave 4
    assert (notes[0].get("PitchStep"), notes[0].get("PitchOctave")) == ("0", "4")


def test_ccs_roundtrip(tmp_path: Path) -> None:
    song = make_song()
    path = cevio.write_ccs(song, tmp_path / "song.ccs")
    back = cevio.read_ccs(path)
    assert back.bpm == 128.0
    assert rows(back, 0) == rows(song, 0)
    assert rows(back, 1) == rows(song, 1)
    assert back.tracks[1].language == "mandarin"


# --------------------------------------------------------------------------- #
# MIDI
# --------------------------------------------------------------------------- #
def test_midi_roundtrip(tmp_path: Path) -> None:
    song = make_song()
    path = write_midi(song, tmp_path / "song.mid")
    back = read_midi(path)
    assert back.bpm == 128.0
    assert (back.beats_per_bar, back.beat_unit) == (4, 4)
    assert len(back.tracks) == 2
    assert rows(back, 0) == rows(song, 0)
    assert rows(back, 1) == rows(song, 1)
    assert [t.name for t in back.tracks] == ["Lead", "Harmony"]


def test_midi_header_is_format_1(tmp_path: Path) -> None:
    path = write_midi(make_song(), tmp_path / "song.mid")
    data = path.read_bytes()
    assert data[:4] == b"MThd"
    assert int.from_bytes(data[8:10], "big") == 1  # format 1
    assert int.from_bytes(data[10:12], "big") == 3  # conductor + 2 tracks
    assert int.from_bytes(data[12:14], "big") == 480


# --------------------------------------------------------------------------- #
# cross-format
# --------------------------------------------------------------------------- #
def test_every_native_format_agrees(tmp_path: Path) -> None:
    song = make_song()
    reference = rows(song, 0)
    for engine, suffix in (("synthv", ".svp"), ("vocaloid", ".vsqx"), ("cevio", ".ccs")):
        info = export_song(song, engine, tmp_path / "out")
        assert Path(info["path"]).suffix == suffix
        assert rows(import_song(info["path"]), 0) == reference


def test_voisona_gets_an_svp(tmp_path: Path) -> None:
    info = export_song(make_song(), "voisona", tmp_path / "out")
    assert Path(info["path"]).suffix == ".svp"
    assert "VoiSona imports it directly" in info["note"]


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #
def call(server, tool: str, **args):
    result = asyncio.run(server.call_tool(tool, args))
    payload = getattr(result, "structured_content", None)
    assert payload is not None, f"{tool} returned no structured content"
    return payload


@pytest.fixture()
def server(tmp_path: Path):
    return build_server(Store(home=tmp_path))


def test_tool_flow_end_to_end(server, tmp_path: Path) -> None:
    created = call(server, "project_create", name="Flow", bpm=100)
    assert created["ok"] is True

    added = call(
        server, "notes_add", project="Flow", track="Vocals",
        notes=[{"onset": 0, "duration": 1, "pitch": 60},
               {"onset": 1, "duration": 1, "pitch": 64}],
    )
    assert added["added"] == 2

    filled = call(server, "lyrics_fill", project="Flow", track="Vocals", text="あ-い")
    assert filled["assigned"] == 2

    shown = call(server, "project_show", project="Flow")
    assert shown["summary"]["tracks"][0]["notes"] == 2
    assert "preview" in shown

    exported = call(server, "export", project="Flow")
    assert exported["ok"] is True
    assert {f["engine"] for f in exported["files"]} == {
        "synthv", "vocaloid", "cevio", "voisona"
    }
    assert exported["errors"] == []
    for item in exported["files"]:
        assert Path(item["path"]).exists()


def test_tool_errors_are_reported_not_raised(server) -> None:
    missing = call(server, "project_show", project="nope")
    assert missing["ok"] is False and "no project" in missing["error"]

    call(server, "project_create", name="Tiny")
    bad = call(server, "notes_add", project="Tiny", track="Vocals", notes=[{"onset": 0}])
    assert bad["ok"] is False and "missing" in bad["error"]

    unknown = call(server, "export", project="Tiny", engines=["ableton"])
    assert unknown["ok"] is False and "unknown engine" in unknown["error"]


def test_tool_note_editing(server) -> None:
    call(server, "project_create", name="Edit")
    call(
        server, "notes_add", project="Edit", track="Vocals",
        notes=[{"onset": 0, "duration": 1, "pitch": 60, "lyric": "a"},
               {"onset": 1, "duration": 1, "pitch": 62, "lyric": "i"}],
    )
    call(server, "note_update", project="Edit", track="Vocals", index=1, pitch=67)
    transposed = call(server, "transpose", project="Edit", track="Vocals", semitones=2)
    assert transposed["ok"] is True

    listed = call(server, "notes_list", project="Edit", track="Vocals")
    assert [n["pitch"] for n in listed["notes"]] == [62, 69]

    call(server, "quantize", project="Edit", track="Vocals", grid=0.5)
    call(server, "shift", project="Edit", track="Vocals", beats=4)
    listed = call(server, "notes_list", project="Edit", track="Vocals")
    assert [n["onset"] for n in listed["notes"]] == [4.0, 5.0]

    deleted = call(server, "note_delete", project="Edit", track="Vocals", indexes=[0])
    assert deleted["remaining"] == 1


def test_tool_import_export_cycle(server, tmp_path: Path) -> None:
    song = make_song()
    svp = synthv.write_svp(song, tmp_path / "import-me.svp")
    imported = call(server, "project_import", path=str(svp), name="Imported")
    assert imported["ok"] is True
    assert imported["summary"]["total_beats"] == pytest.approx(4.0)

    listed = call(server, "project_list")
    assert any(p["name"] == "Imported" for p in listed["projects"])


def test_tool_engines_reports_channels(server) -> None:
    found = call(server, "engines")
    assert found["ok"] is True
    channels = {e["engine"]: e["channel"] for e in found["engines"]}
    assert set(channels) == {"synthv", "vocaloid", "cevio", "voisona"}
    assert "Lua agent" in channels["synthv"]
    assert "injected agent" in channels["vocaloid"]
    assert found["workspace"]["home"]
