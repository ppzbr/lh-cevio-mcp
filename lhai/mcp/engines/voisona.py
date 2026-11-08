"""VoiSona adapter: it is the easy one, because it imports everything.

``VoiSona.exe`` advertises ``.svp``, ``.ccs``, ``.mid`` and ``.musicxml`` in its
file filters, so there is no VoiSona project writer to get wrong: the best
strategy is to write the Synthesizer V project (which carries phonemes and
control curves) and let VoiSona open it.

That is what :func:`prepare` does.  ``.ccs`` is offered second because a CeVIO
scenario arrives without curves but with the right casting layout, and MIDI last
because it loses lyrics' phonemes but never fails.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lhai.mcp.engines.base import find_engine
from lhai.mcp.ir import Song

__all__ = ["installed_voices", "prepare"]

#: Preference order when the caller does not name a format.
FORMAT_ORDER = (".svp", ".ccs", ".mid")


def installed_voices() -> list[dict[str, Any]]:
    """Singer packages installed under ``dic/Singer`` (plus packaged extras)."""
    install = find_engine("voisona")
    if install.root is None:
        return []
    out: list[dict[str, Any]] = []
    for folder_name in ("dic/Singer", "dic", "voices"):
        folder = install.root / folder_name
        if not folder.exists():
            continue
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                out.append({"name": entry.name, "path": str(entry), "folder": folder_name})
    return out


def prepare(song: Song, out_dir: str | Path, *, fmt: str = ".svp") -> tuple[Path, str]:
    """Write the highest-fidelity file VoiSona can import.

    Returns ``(path, note)`` where ``note`` explains what was and was not
    carried over, so the caller can tell the user rather than guess.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = fmt if fmt.startswith(".") else f".{fmt}"
    if suffix not in FORMAT_ORDER:
        raise ValueError(f"VoiSona cannot import {suffix}; use one of {FORMAT_ORDER}")
    target = out_dir / f"{_safe(song.name)}{suffix}"

    if suffix == ".svp":
        from lhai.mcp.engines.synthv import write_svp

        write_svp(song, target)
        return target, "svp keeps lyrics, phonemes and control curves; VoiSona imports it directly"
    if suffix == ".ccs":
        from lhai.mcp.engines.cevio import write_ccs

        write_ccs(song, target)
        return target, "ccs keeps the casting layout but no curves"
    from lhai.mcp.midiio import write_midi

    write_midi(song, target)
    return target, "mid keeps notes and lyrics, loses phoneme overrides and curves"


def _safe(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in str(name))
    return (cleaned.strip() or "song")[:80]
