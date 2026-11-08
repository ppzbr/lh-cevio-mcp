"""Engine registry: one import, every adapter, no dispatch logic in the server.

The MCP tools only ever call :func:`export_song`, :func:`import_song` and
:func:`voices`, so adding a fifth engine is a module plus three lines here -
and the tool layer never grows a branch per engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lhai.mcp.engines import base, cevio, synthv, vocaloid, voisona
from lhai.mcp.ir import Song

__all__ = [
    "base",
    "cevio",
    "synthv",
    "vocaloid",
    "voisona",
    "ENGINE_KEYS",
    "export_song",
    "import_song",
    "voices",
    "EXPORT_SUFFIX",
]

ENGINE_KEYS = base.ENGINE_KEYS

#: Native file per engine.  VoiSona writes nothing of its own - it imports - so
#: its export is an ``.svp`` by design, not by laziness.
EXPORT_SUFFIX: dict[str, str] = {
    "synthv": ".svp",
    "vocaloid": ".vsqx",
    "cevio": ".ccs",
    "voisona": ".svp",
}


def _safe(name: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch in "-_ ") else "_" for ch in str(name))
    return (cleaned.strip() or "song")[:80]


def export_song(
    song: Song,
    engine: str,
    out_dir: str | Path,
    *,
    filename: str | None = None,
    voice_id: str | None = None,
    voice_name: str = "",
    fmt: str | None = None,
) -> dict[str, Any]:
    """Write ``song`` in the engine's native format.  Returns a report dict."""
    if engine not in ENGINE_KEYS:
        raise KeyError(f"unknown engine {engine!r}; expected one of {ENGINE_KEYS}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if engine == "voisona":
        path, note = voisona.prepare(song, out_dir, fmt=fmt or EXPORT_SUFFIX[engine])
        return {
            "engine": engine,
            "path": str(path),
            "format": path.suffix.lower(),
            "note": note,
            "installed": base.find_engine(engine).installed,
        }

    suffix = fmt or EXPORT_SUFFIX[engine]
    if not suffix.startswith("."):
        suffix = "." + suffix
    stem = _safe(filename or song.name)
    target = out_dir / f"{stem}{suffix}"

    if engine == "synthv":
        synthv.write_svp(song, target)
        note = "v153 project: notes, lyrics, phonemes, voice database (no pitch/vibrato automation)"
    elif engine == "vocaloid":
        vocaloid.write_vsqx(
            song,
            target,
            voice_id=voice_id or vocaloid.DEFAULT_VOICE_ID,
            voice_name=voice_name or (song.tracks[0].voice if song.tracks else ""),
        )
        note = (
            "vsq4 XML: notes, lyrics, phonemes; language comes from the voicebank. "
            "Pass voice_id to avoid the editor's default singer."
        )
    elif engine == "cevio":
        cevio.write_ccs(song, target)
        note = (
            "CeVIO scenario XML: notes, lyrics, tempo, time signature; "
            "the singer is the editor's current cast (no Cast block is written)"
        )
    else:  # pragma: no cover - guarded above
        raise KeyError(engine)

    return {
        "engine": engine,
        "path": str(target),
        "format": suffix,
        "note": note,
        "installed": base.find_engine(engine).installed,
    }


def import_song(path: str | Path) -> Song:
    """Read any supported project file back into the IR.

    ``.svp`` / ``.vsqx`` / ``.ccs`` / ``.mid`` are the formats the four engines
    actually produce, so this is also the migration path between them.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix == ".svp":
        return synthv.read_svp(path)
    if suffix in (".vsqx", ".vsq"):
        return vocaloid.read_vsqx(path)
    if suffix == ".ccs":
        return cevio.read_ccs(path)
    if suffix in (".mid", ".midi"):
        from lhai.mcp.midiio import read_midi

        return read_midi(path)
    raise ValueError(
        f"unsupported project format {suffix!r}; supported: .svp .vsqx .vsq .ccs .mid .midi"
    )


def voices(engine: str, folder: str = "") -> list[dict[str, Any]]:
    if engine == "synthv":
        return synthv.installed_voices([folder] if folder else None)
    if engine == "vocaloid":
        return vocaloid.installed_voices(folder or None)
    if engine == "cevio":
        return cevio.installed_voices()
    if engine == "voisona":
        return voisona.installed_voices()
    raise KeyError(f"unknown engine {engine!r}")
