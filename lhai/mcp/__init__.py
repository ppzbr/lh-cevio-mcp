"""Singing-engine MCP server: CeVIO AI, VoiSona, Synthesizer V, VOCALOID.

The package exists because the four engines share one hard problem and nothing
else: a singer needs a score, and every editor stores that score differently.
So the shape is

    Song (beats, MIDI pitch, one syllable per note)  <- lhai.mcp.ir
      -> adapters (one module per engine, at most one file format each)
      -> MCP tools (edit, export, open, or push live)

Nothing here is engine-specific at the tool layer, and nothing in the IR is
engine-specific either, which is why ``Song`` can also be handed to the
LH-Score compiler in :mod:`lhai.core.lh_score` for training data.

Quick start from a client::

    engines()                                  # what is installed
    project_create("demo", bpm=128)
    notes_add("demo", "Vocals", [{"onset": 0, "duration": 1, "pitch": 60}])
    lyrics_fill("demo", "Vocals", "ka-ze-ni-no-ru")
    export("demo")                             # svp + vsqx + ccs + mid
    open_in("demo", "synthv")
"""

from __future__ import annotations

__all__ = [
    "Song",
    "Track",
    "SNote",
    "Store",
    "split_lyrics",
    "build_server",
    "mcp",
]

from lhai.mcp.ir import SNote, Song, Track, split_lyrics
from lhai.mcp.store import Store


def build_server():
    """Lazily build the MCP server so importing the package needs no ``mcp``."""
    from lhai.mcp.server import build_server as _build

    return _build()


def __getattr__(name: str):
    if name == "mcp":
        from lhai.mcp.server import mcp as _mcp

        return _mcp
    raise AttributeError(name)
