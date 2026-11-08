"""Engine-neutral song IR - the one thing every adapter agrees on.

A song is a *score*, not a project file.  Each engine (Synthesizer V, VOCALOID,
CeVIO AI, VoiSona) stores the same information in a different dialect:

============================  ==========================================
engine                        musical unit
============================  ==========================================
Synthesizer V ``.svp``        1 quarter note = 705 600 000 "blick"
VOCALOID ``.vsqx``            1 quarter note = ``resolution`` ticks (480)
CeVIO ``.ccs``                1 quarter note = 960 clock + 3840 pre-measure
VoiSona                        imports ``.svp`` / ``.ccs`` / MIDI
============================  ==========================================

So the IR stores **beats** - floats, no grid, no tempo scaling - and every
adapter converts once at its own boundary.  Beats are what a musician edits;
ticks are what a file format serialises.  Mixing the two is how round-trips
grow a one-tick drift per save.

Pitch is a MIDI note number (0..127).  ``pitch == -1`` is a rest, which is the
same convention ``lhai.core.types.Note`` already uses, so a score built here can
be handed to the LH-Score compiler without translation.

Time signature and tempo live on the song, because every engine here has one
global tempo map and a per-song meter.  Anything a specific engine can express
and the others cannot (vocal modes, phoneme overrides, control curves) is
carried in ``SNote.phonemes`` / ``Track.controls`` and dropped by the adapters
that cannot use it - never silently mangled into a different field.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "SNote",
    "Track",
    "Song",
    "split_lyrics",
    "clamp_pitch",
    "beat_to_tick",
    "CHINESE_TAG",
]

#: Language tags accepted by every engine's ``database.language`` field.  The
#: engines use these exact lowercase words; a BCP-47 tag like ``zh-CN`` is
#: mapped onto them rather than passed through, because a wrong tag silently
#: loads the wrong dictionary.
LANGUAGES: tuple[str, ...] = (
    "mandarin",
    "cantonese",
    "japanese",
    "english",
    "korean",
    "spanish",
    "portuguese",
    "italian",
)

CHINESE_TAG = "mandarin"

_LANG_ALIASES: dict[str, str] = {
    "zh": "mandarin",
    "zh-cn": "mandarin",
    "zh-tw": "mandarin",
    "cn": "mandarin",
    "chinese": "mandarin",
    "cmn": "mandarin",
    "yue": "cantonese",
    "zh-hk": "cantonese",
    "ja": "japanese",
    "jp": "japanese",
    "en": "english",
    "en-us": "english",
    "en-gb": "english",
    "ko": "korean",
    "kr": "korean",
    "es": "spanish",
    "pt": "portuguese",
    "it": "italian",
}

#: Code points that are one syllable per character.  A Chinese/Japanese/Korean
#: lyric line usually arrives as one unspaced string, so ``split_lyrics`` has to
#: know where a character boundary is also a syllable boundary.
_CJK_START = "\u3040\u30a0\u3400\u4e00\uac00\uf900"
_CJK_RANGES: tuple[tuple[str, str], ...] = (
    ("\u3040", "\u309f"),  # hiragana
    ("\u30a0", "\u30ff"),  # katakana
    ("\u3400", "\u4dbf"),  # CJK ext A
    ("\u4e00", "\u9fff"),  # CJK unified
    ("\uac00", "\ud7af"),  # hangul syllables
    ("\uf900", "\ufaff"),  # CJK compatibility
)


def _is_cjk(ch: str) -> bool:
    return any(lo <= ch <= hi for lo, hi in _CJK_RANGES)


def _is_kana(ch: str) -> bool:
    return "\u3040" <= ch <= "\u30ff"


def normalize_language(value: str | None, *, default: str = "japanese") -> str:
    """Map ``zh-CN`` / ``EN`` / ``中文`` onto an engine language tag."""
    if not value:
        return default
    raw = str(value).strip()
    lowered = raw.lower().replace("_", "-")
    if lowered in LANGUAGES:
        return lowered
    if lowered in _LANG_ALIASES:
        return _LANG_ALIASES[lowered]
    if raw in ("中文", "汉语", "普通话", "國語"):
        return "mandarin"
    if raw in ("日本語", "日语"):
        return "japanese"
    if raw in ("한국어", "韓国語"):
        return "korean"
    head = lowered.split("-")[0]
    return _LANG_ALIASES.get(head, default)


def clamp_pitch(pitch: Any) -> int:
    return max(-1, min(127, int(round(float(pitch)))))


def beat_to_tick(beats: float, ticks_per_quarter: int) -> int:
    """Beats -> integer ticks, rounded once (never accumulated)."""
    return int(round(float(beats) * float(ticks_per_quarter)))


# --------------------------------------------------------------------------- #
# lyric splitting
# --------------------------------------------------------------------------- #
_HYPHEN_SPLIT = re.compile(r"\s*[-‐‑–—]\s*")
_BAR = re.compile(r"[\n\r|｜/]")


def split_lyrics(text: str, *, latin: str = "auto") -> list[str]:
    """Split a lyric line into syllables, one per note.

    The rule order matters, because each test is a *stronger* statement than
    the next one:

    1. ``-`` and ``|`` are explicit syllable/phrase separators - the writer
       said where the boundary is, so nothing else may override it.
    2. Whitespace-separated tokens are words; a word in pure CJK is one
       syllable per character (``你好`` is two notes).
    3. A Latin *word* stays whole, because the engine's G2P finds its vowels far
       better than any regex can.  ``latin="syllables"`` opts into the vowel
       group split for callers who want ``hello`` -> ``he llo``.
    4. An unspaced Latin string with no separators at all has nothing else to
       go on, so it is split on vowel groups regardless (``kanade`` ->
       ``ka na de``).  Without this a one-word lyric would consume a whole
       melody.

    Kana is deliberately *not* split per character: ``さくら`` is one entry in a
    lyric line even when it spans two notes, and both SynthV and CeVIO prefer
    whole-word kana entries.
    """
    raw = unicodedata.normalize("NFC", str(text or ""))
    raw = raw.replace("\u3000", " ").replace("_", " ").strip()
    if not raw:
        return []

    phrases = [p.strip() for p in _BAR.split(raw) if p.strip()]
    if not phrases:
        phrases = [raw]

    out: list[str] = []
    for phrase in phrases:
        if _HYPHEN_SPLIT.search(phrase):
            for part in _HYPHEN_SPLIT.split(phrase):
                part = part.strip()
                if part:
                    out.extend(_segment(part, latin=latin, explicit=True))
        else:
            out.extend(_segment(phrase, latin=latin, explicit=len(phrases) > 1))
    return out


def _segment(token: str, *, latin: str, explicit: bool) -> list[str]:
    """One phrase/chunk -> syllables."""
    if " " in token or "\t" in token:
        out: list[str] = []
        for word in token.split():
            # Whitespace *is* an explicit boundary: a word the caller separated
            # out stays one note instead of being vowel-split.
            out.extend(_segment(word, latin=latin, explicit=True))
        return out
    if all(_is_cjk(ch) for ch in token):
        if all(_is_kana(ch) for ch in token) and len(token) > 1:
            return [token]
        return list(token)
    if any(_is_cjk(ch) for ch in token):
        # Mixed script (e.g. "君のvoice"): split at script changes.
        out, buf = [], ""
        buf_cjk = None
        for ch in token:
            is_cjk = _is_cjk(ch)
            if buf and is_cjk != buf_cjk:
                out.append(buf)
                buf = ""
            buf += ch
            buf_cjk = is_cjk
        if buf:
            out.append(buf)
        return out
    if latin == "syllables" or not explicit:
        return _latin_syllables(token)
    return [token]


_VOWELS = "aeiouyAEIOUYàâäéèêëîïôöùûü"


def _latin_syllables(word: str) -> list[str]:
    """Vowel-group split: ``kanade`` -> ``ka na de``.

    Not a real English syllabifier - that needs a dictionary and the engine has
    one.  This exists so a Latin lyric with no separators still produces one
    note per vowel group instead of one note per line.
    """
    if len(word) <= 2 or not any(ch in _VOWELS for ch in word):
        return [word]
    groups: list[str] = []
    buf = ""
    for i, ch in enumerate(word):
        buf += ch
        nxt = word[i + 1] if i + 1 < len(word) else ""
        if ch in _VOWELS and nxt and nxt not in _VOWELS:
            # keep a final consonant for the next onset, close the syllable
            groups.append(buf)
            buf = ""
    if buf:
        groups.append(buf)
    return groups or [word]


# --------------------------------------------------------------------------- #
# note
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SNote:
    """One sung note.  ``onset``/``duration`` are beats from the song start."""

    onset: float
    duration: float
    pitch: int = 60
    lyric: str = "a"
    #: Optional phonetic override.  Synthesizer V and VOCALOID both accept a
    #: space-separated phoneme string; ``""`` means "let the engine decide from
    #: the lyric", which is what you want 95% of the time.
    phonemes: str = ""
    #: Per-note language override (mixed-lingual lines).  Empty = track default.
    language: str = ""

    def __post_init__(self) -> None:
        self.onset = float(self.onset)
        self.duration = float(self.duration)
        self.pitch = clamp_pitch(self.pitch)
        self.lyric = str(self.lyric or "").strip()
        self.phonemes = str(self.phonemes or "").strip()

    @property
    def end(self) -> float:
        return self.onset + self.duration

    @property
    def is_rest(self) -> bool:
        return self.pitch < 0 or self.lyric in ("", "R", "r")

    def validate(self) -> list[str]:
        issues: list[str] = []
        if self.duration <= 0:
            issues.append(f"note at beat {self.onset:.3f} has duration {self.duration}")
        if self.onset < 0:
            issues.append(f"note at beat {self.onset:.3f} starts before the song")
        if any(ch.isspace() for ch in self.lyric):
            issues.append(f"lyric {self.lyric!r} contains whitespace (one syllable per note)")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "onset": round(self.onset, 6),
            "duration": round(self.duration, 6),
            "pitch": self.pitch,
            "lyric": self.lyric,
            "phonemes": self.phonemes,
            "language": self.language,
        }

    @classmethod
    def from_any(cls, item: "SNote | dict[str, Any]") -> "SNote":
        if isinstance(item, SNote):
            return item
        missing = {"onset", "duration"} - set(item)
        if missing:
            raise ValueError(f"note is missing {sorted(missing)}: {item!r}")
        return cls(
            onset=item["onset"],
            duration=item["duration"],
            pitch=item.get("pitch", 60),
            lyric=item.get("lyric", item.get("text", "a")),
            phonemes=item.get("phonemes", ""),
            language=item.get("language", ""),
        )


# --------------------------------------------------------------------------- #
# track
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Track:
    """One singer on one staff."""

    name: str = "Track 1"
    #: Engine-side voice name, exactly as the engine spells it ("Sheena 2",
    #: "HATSUNE MIKU V6").  Empty means "whatever the engine has selected" -
    #: better than guessing, because a wrong voice name makes Synthesizer V
    #: silently render with a default.
    voice: str = ""
    language: str = "japanese"
    notes: list[SNote] = field(default_factory=list)
    gain_db: float = 0.0
    pan: float = 0.0  # -1..1
    muted: bool = False
    solo: bool = False
    #: Engine-specific control curves, ``{name: [(beat, value), ...]}`` with
    #: value in the engine's own range.  Written verbatim where supported.
    controls: dict[str, list[list[float]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.language = normalize_language(self.language)

    @property
    def end_beat(self) -> float:
        return max((n.end for n in self.notes), default=0.0)

    def sorted_notes(self) -> list[SNote]:
        return sorted(self.notes, key=lambda n: (n.onset, n.pitch))

    def validate(self) -> list[str]:
        issues = [f"{self.name}: {m}" for n in self.notes for m in n.validate()]
        if not (1 <= len(self.name) <= 60):
            issues.append(f"track name {self.name!r} must be 1..60 characters")
        return issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "voice": self.voice,
            "language": self.language,
            "gain_db": self.gain_db,
            "pan": self.pan,
            "muted": self.muted,
            "solo": self.solo,
            "controls": self.controls,
            "notes": [n.to_dict() for n in self.notes],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track":
        return cls(
            name=data.get("name", "Track"),
            voice=data.get("voice", ""),
            language=data.get("language", "japanese"),
            notes=[SNote.from_any(n) for n in data.get("notes", [])],
            gain_db=float(data.get("gain_db", 0.0)),
            pan=float(data.get("pan", 0.0)),
            muted=bool(data.get("muted", False)),
            solo=bool(data.get("solo", False)),
            controls={k: list(v) for k, v in (data.get("controls") or {}).items()},
        )


# --------------------------------------------------------------------------- #
# song
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Song:
    """A whole score: the thing the MCP tools read and write."""

    name: str = "untitled"
    bpm: float = 120.0
    beats_per_bar: int = 4
    beat_unit: int = 4
    tracks: list[Track] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- derived ---------------------------------------------------------- #
    @property
    def total_beats(self) -> float:
        return max((t.end_beat for t in self.tracks), default=0.0)

    @property
    def bars(self) -> float:
        return self.total_beats / float(self.beats_per_bar) if self.beats_per_bar else 0.0

    def seconds(self, beats: float | None = None) -> float:
        return (self.total_beats if beats is None else beats) * 60.0 / float(self.bpm)

    def track(self, ref: "str | int") -> Track:
        """Resolve a track by index or by name (case-insensitive)."""
        if isinstance(ref, int) or (isinstance(ref, str) and ref.lstrip("-").isdigit()):
            idx = int(ref)
            if not -len(self.tracks) <= idx < len(self.tracks):
                raise IndexError(f"track index {idx} out of range ({len(self.tracks)} tracks)")
            return self.tracks[idx]
        lowered = str(ref).strip().lower()
        for t in self.tracks:
            if t.name.lower() == lowered:
                return t
        raise KeyError(f"no track named {ref!r}; have {[t.name for t in self.tracks]}")

    def track_index(self, track: Track) -> int:
        return self.tracks.index(track)

    # -- validation ------------------------------------------------------- #
    def validate(self, *, strict: bool = True) -> list[str]:
        issues: list[str] = []
        if not (20.0 <= float(self.bpm) <= 400.0):
            issues.append(f"bpm {self.bpm} outside 20..400")
        if not (1 <= int(self.beats_per_bar) <= 32):
            issues.append(f"beats_per_bar {self.beats_per_bar} outside 1..32")
        if int(self.beat_unit) not in (2, 4, 8, 16):
            issues.append(f"beat_unit {self.beat_unit} must be 2, 4, 8 or 16")
        if not self.tracks:
            issues.append("song has no tracks")
        for t in self.tracks:
            issues.extend(t.validate())
        if strict and issues:
            raise ValueError("; ".join(issues))
        return issues

    def overlaps(self) -> list[str]:
        """Overlapping notes per track - legal in every engine, usually a bug."""
        out: list[str] = []
        for t in self.tracks:
            notes = t.sorted_notes()
            for a, b in zip(notes, notes[1:]):
                if a.end > b.onset + 1e-9:
                    out.append(
                        f"{t.name}: note at {a.onset:.3f} overlaps the next at {b.onset:.3f}"
                    )
        return out

    # -- serialisation ---------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "lh-singing-song/1",
            "name": self.name,
            "bpm": self.bpm,
            "beats_per_bar": self.beats_per_bar,
            "beat_unit": self.beat_unit,
            "meta": self.meta,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Song":
        return cls(
            name=data.get("name", "untitled"),
            bpm=float(data.get("bpm", 120.0)),
            beats_per_bar=int(data.get("beats_per_bar", 4)),
            beat_unit=int(data.get("beat_unit", 4)),
            tracks=[Track.from_dict(t) for t in data.get("tracks", [])],
            meta=dict(data.get("meta") or {}),
        )

    # -- reporting -------------------------------------------------------- #
    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bpm": self.bpm,
            "meter": f"{self.beats_per_bar}/{self.beat_unit}",
            "tracks": [
                {
                    "index": i,
                    "name": t.name,
                    "voice": t.voice,
                    "language": t.language,
                    "notes": len(t.notes),
                    "beats": round(t.end_beat, 3),
                }
                for i, t in enumerate(self.tracks)
            ],
            "total_beats": round(self.total_beats, 3),
            "total_bars": round(self.bars, 3),
            "seconds": round(self.seconds(), 3),
            "issues": self.validate(strict=False),
            "overlaps": self.overlaps()[:10],
        }

    def piano_roll(self, track: "str | int" = 0, *, width: int = 72) -> str:
        """Tiny ASCII preview so a text-only client can *see* the melody."""
        t = self.track(track)
        notes = t.sorted_notes()
        if not notes:
            return f"{t.name}: (empty)"
        lo = min(n.pitch for n in notes if n.pitch >= 0)
        hi = max(n.pitch for n in notes if n.pitch >= 0)
        lo, hi = min(lo, hi - 1), max(hi, lo + 1)
        span = self.total_beats or 1.0
        step = span / width
        rows: list[str] = []
        for pitch in range(hi, lo - 1, -1):
            cells = [" "] * width
            for n in notes:
                if n.pitch != pitch:
                    continue
                a = int(n.onset / step) if step else 0
                b = max(a + 1, int((n.end) / step))
                for i in range(a, min(b, width)):
                    cells[i] = "=" if (n.end - n.onset) > step * 2 else "-"
                if 0 <= a < width:
                    cells[a] = "["
            rows.append(f"{pitch:3d} |" + "".join(cells) + "|")
        marks = [" "] * width
        for bar in range(0, int(span) + 1, max(1, int(self.beats_per_bar))):
            i = int(bar / step) if step else 0
            if 0 <= i < width:
                marks[i] = "|"
        rows.append("    +" + "".join(marks) + "+")
        rows.append(
            f"    bar {self.beats_per_bar}/{self.beat_unit}  "
            f"{span:.2f} beats  {self.bpm:g} bpm  {len(notes)} notes"
        )
        return "\n".join(rows)


def notes_from_rows(rows: Iterable[dict[str, Any]]) -> list[SNote]:
    return [SNote.from_any(r) for r in rows]
