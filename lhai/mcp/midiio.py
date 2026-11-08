"""Standard MIDI File read/write, standard library only.

MIDI is the lingua franca of this package: every one of the four engines can
open a ``.mid``, so it is the fallback that always works even when an engine's
native project format is stricter than the documentation claims.  Keeping it
dependency-free (no ``mido``) is deliberate - the MCP server must start on a
machine where nothing but the runtime is installed.

What is written:

* format 1 (one meta track + one track per song track),
* ``track_name``, ``set_tempo``, ``time_signature``,
* a ``lyrics`` meta event immediately before each ``note_on`` - that is the
  convention every singing engine reads when importing MIDI,
* note on/off pairs on channel 0, so a singer import finds them.

What is *not* written: pitch bend, CC curves, SMPTE timing.  They are not
portable across these engines and pretending otherwise produces files that open
"correctly" and sound wrong.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from lhai.mcp.ir import SNote, Song, Track, beat_to_tick

__all__ = ["MIDI_PPQ", "write_midi", "read_midi"]

#: Ticks per quarter note.  480 is what every engine's own importer expects and
#: what VSQX uses, so no rounding is introduced between the two.
MIDI_PPQ = 480


# --------------------------------------------------------------------------- #
# var-length quantities
# --------------------------------------------------------------------------- #
def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError(f"negative delta time {value}")
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _read_vlq(fp: BinaryIO) -> int:
    value = 0
    for _ in range(4):
        byte = fp.read(1)
        if not byte:
            raise EOFError("truncated variable-length quantity")
        value = (value << 7) | (byte[0] & 0x7F)
        if not byte[0] & 0x80:
            return value
    raise ValueError("variable-length quantity longer than 4 bytes")


def _meta(kind: int, payload: bytes) -> bytes:
    return bytes([0xFF, kind]) + _vlq(len(payload)) + payload


def _text(kind: int, text: str) -> bytes:
    return _meta(kind, text.encode("utf-8"))


def _chunk(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack(">I", len(body)) + body


# --------------------------------------------------------------------------- #
# writer
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _Event:
    tick: int
    order: int
    payload: bytes


def write_midi(song: Song, path: str | Path, *, ppq: int = MIDI_PPQ) -> Path:
    """Serialise ``song`` to a format-1 SMF.  Returns the path written."""
    path = Path(path)
    tracks = song.tracks or [Track(name="Track 1")]

    conductor = bytearray()
    conductor += _text(0x03, song.name[:127])
    conductor += _text(0x01, f"lh-ai-studio mcp; {len(song.tracks)} vocal track(s)")
    conductor += b"\x00" + _meta(0x51, _tempo_payload(song.bpm))
    signature = bytes([song.beats_per_bar, _beat_unit_pow(song.beat_unit), 24, 8])
    conductor += b"\x00" + _meta(0x58, signature)
    conductor += b"\x00" + _meta(0x2F, b"")

    track_chunks = [_chunk(b"MTrk", bytes(conductor))]

    for index, track in enumerate(tracks):
        events: list[_Event] = []
        order = 0
        for note in track.sorted_notes():
            on = beat_to_tick(note.onset, ppq)
            off = max(on + 1, beat_to_tick(note.end, ppq))
            pitch = 127 if note.pitch < 0 else note.pitch
            # lyric first, so an importer that pairs "last lyric before note_on"
            # (all of them do) picks the right syllable.
            if note.lyric:
                events.append(_Event(on, order, _text(0x05, note.lyric[:60])))
                order += 1
            events.append(_Event(on, order, bytes([0x90, pitch, 100])))
            order += 1
            events.append(_Event(off, order, bytes([0x80, pitch, 0])))
            order += 1

        events.sort(key=lambda e: (e.tick, e.order))
        body = bytearray(b"\x00" + _text(0x03, track.name[:127]))
        body += b"\x00" + _text(0x04, f"token={index}")
        previous = 0
        for event in events:
            body += _vlq(event.tick - previous) + event.payload
            previous = event.tick
        body += _vlq(0) + _meta(0x2F, b"")
        track_chunks.append(_chunk(b"MTrk", bytes(body)))

    header = _chunk(b"MThd", struct.pack(">HHH", 1, len(track_chunks), ppq))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + b"".join(track_chunks))
    return path


def _tempo_payload(bpm: float) -> bytes:
    micros = int(round(60_000_000.0 / float(bpm)))
    return struct.pack(">I", micros)[1:]


def _beat_unit_pow(beat_unit: int) -> int:
    return {1: 0, 2: 1, 4: 2, 8: 3, 16: 4}.get(int(beat_unit), 2)


# --------------------------------------------------------------------------- #
# reader
# --------------------------------------------------------------------------- #
def read_midi(path: str | Path) -> Song:
    """Parse an SMF into a :class:`Song`.  Tolerant, not clever.

    The reader keeps only what the IR can represent: notes, lyrics, tempo and
    the meter of bar 1.  Tempo *changes* collapse to the first tempo, because a
    score that changes tempo mid-song is a different kind of project than the
    one this IR models; ignoring the change would silently mis-time the notes,
    so the value is recorded in ``meta`` and the first tempo is used.
    """
    path = Path(path)
    data = path.read_bytes()
    if data[:4] != b"MThd":
        raise ValueError(f"{path} is not a Standard MIDI File")
    header_len = struct.unpack(">I", data[4:8])[0]
    _fmt, ntracks, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise ValueError("SMPTE time division is not supported; use PPQ timing")
    pos = 8 + header_len

    song = Song(name=path.stem)
    tempos: list[tuple[int, float]] = []
    meters: list[tuple[int, int, int]] = []
    track_blobs: list[bytes] = []
    while pos + 8 <= len(data):
        tag = data[pos : pos + 4]
        length = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + length]
        pos += 8 + length
        if tag == b"MTrk":
            track_blobs.append(body)

    for blob in track_blobs:
        notes, name, lyrics = _parse_track(blob, division, tempos, meters)
        if name and not song.name:
            song.name = name
        if notes:
            song.tracks.append(Track(name=name or f"Track {len(song.tracks) + 1}", notes=notes))

    if tempos:
        tempos.sort()
        song.bpm = tempos[0][1]
        if len({round(t[1], 4) for t in tempos}) > 1:
            song.meta["tempo_changes"] = [{"tick": t, "bpm": b} for t, b in tempos]
            song.meta["tempo_note"] = "only the first tempo was applied"
    if meters:
        meters.sort()
        first_tick, num, den = meters[0]
        song.beats_per_bar, song.beat_unit = num, den
        _ = first_tick
    song.meta["source"] = str(path)
    song.meta["source_format"] = "midi"
    return song


def _parse_track(
    blob: bytes,
    division: int,
    tempos: list[tuple[int, float]],
    meters: list[tuple[int, int, int]],
) -> tuple[list[SNote], str, list[str]]:
    import io

    fp = io.BytesIO(blob)
    tick = 0
    status = 0
    pending_lyric = ""
    name = ""
    notes: list[SNote] = []
    open_notes: dict[int, tuple[int, str]] = {}

    def beats(t: int) -> float:
        return t / float(division)

    while True:
        try:
            delta = _read_vlq(fp)
        except EOFError:
            break
        tick += delta
        byte = fp.read(1)
        if not byte:
            break
        if byte[0] & 0x80:
            status = byte[0]
        else:
            fp.seek(-1, io.SEEK_CUR)  # running status
        kind = status & 0xF0

        if status == 0xFF:
            meta_kind = fp.read(1)[0]
            length = _read_vlq(fp)
            payload = fp.read(length)
            if meta_kind == 0x2F:
                break
            if meta_kind == 0x03 and not name:
                name = payload.decode("utf-8", "replace")
            elif meta_kind == 0x05:
                pending_lyric = payload.decode("utf-8", "replace").strip()
            elif meta_kind == 0x51 and len(payload) == 3:
                micros = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                if micros:
                    tempos.append((tick, round(60_000_000.0 / micros, 4)))
            elif meta_kind == 0x58 and len(payload) >= 2:
                meters.append((tick, payload[0] or 4, 1 << (payload[1] or 2)))
            continue
        if status in (0xF0, 0xF7):
            length = _read_vlq(fp)
            fp.read(length)
            continue
        if kind in (0xC0, 0xD0):
            fp.read(1)
            continue

        note = fp.read(1)[0]
        velocity = fp.read(1)[0]
        if kind == 0x90 and velocity > 0:
            open_notes.setdefault(note, (tick, pending_lyric))
            pending_lyric = ""
        elif kind == 0x80 or (kind == 0x90 and velocity == 0):
            started = open_notes.pop(note, None)
            if started is None:
                continue
            begin, lyric = started
            if tick <= begin:
                tick = begin + 1
            notes.append(
                SNote(
                    onset=round(beats(begin), 6),
                    duration=round(beats(tick) - beats(begin), 6),
                    pitch=note,
                    lyric=lyric or "a",
                )
            )

    notes.sort(key=lambda n: (n.onset, n.pitch))
    return notes, name, []


def read_midi_bytes(data: bytes) -> Song:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as fh:
        fh.write(data)
        tmp = fh.name
    try:
        return read_midi(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def song_to_rows(song: Song) -> list[dict[str, Any]]:
    """Flat note table - convenient for JSON results and for diffing exports."""
    rows: list[dict[str, Any]] = []
    for t_index, track in enumerate(song.tracks):
        for n_index, note in enumerate(track.notes):
            rows.append(
                {"track": t_index, "track_name": track.name, "index": n_index, **note.to_dict()}
            )
    return rows
