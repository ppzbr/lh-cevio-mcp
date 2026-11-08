"""Project workspace: the MCP server's memory.

An MCP tool call is stateless, but editing a song is a sequence of calls, so the
song has to live *somewhere* between them.  Three options existed:

* keep songs in the server process (lost on restart, invisible to the user),
* keep them in the MCP client (every tool would take a whole song payload),
* keep them on disk, one JSON file per project, addressed by name.

The third is what this module does.  The files are plain JSON so the user can
open one in an editor, diff two versions, or copy a song to another machine, and
the server never holds state that a restart can lose.

Layout::

    <home>/
      projects/<slug>.json    one song per file, IR schema lh-singing-song/1
      exports/                engine files written by the export tools
      ipc/                    Synthesizer V bridge command/result files
      tmp/                    scratch
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from lhai.mcp.ir import Song

__all__ = ["Store", "home_dir", "slugify"]

_SLUG = re.compile(r"[^a-z0-9._-]+")


def home_dir() -> Path:
    """Workspace root: ``$LH_MCP_HOME`` or ``%LOCALAPPDATA%/lh-ai-studio/singing``."""
    override = os.environ.get("LH_MCP_HOME")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "lh-ai-studio" / "singing"
    return Path.home() / ".lh-ai-studio" / "singing"


def slugify(name: str) -> str:
    cleaned = _SLUG.sub("-", str(name).strip().lower()).strip("-._")
    return (cleaned or "song")[:80]


class Store:
    """Reads and writes songs on disk.  Thread-safe enough for one server."""

    def __init__(self, home: str | Path | None = None) -> None:
        self.home = Path(home) if home else home_dir()
        self.projects = self.home / "projects"
        self.exports = self.home / "exports"
        self.ipc = self.home / "ipc"
        self.tmp = self.home / "tmp"

    # -- paths ------------------------------------------------------------ #
    def ensure(self) -> "Store":
        for folder in (self.projects, self.exports, self.ipc, self.tmp):
            folder.mkdir(parents=True, exist_ok=True)
        return self

    def path_of(self, name: str) -> Path:
        return self.projects / f"{slugify(name)}.json"

    def exists(self, name: str) -> bool:
        return self.path_of(name).exists()

    # -- crud ------------------------------------------------------------- #
    def save(self, song: Song) -> Path:
        self.ensure()
        path = self.path_of(song.name)
        payload = song.to_dict()
        payload["meta"] = {
            **payload.get("meta", {}),
            "saved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def load(self, name: str) -> Song:
        path = self.path_of(name)
        if not path.exists():
            raise FileNotFoundError(
                f"no project {name!r}; have {[p.stem for p in self.list_paths()]}"
            )
        data = json.loads(path.read_text(encoding="utf-8"))
        song = Song.from_dict(data)
        song.meta.setdefault("path", str(path))
        return song

    def delete(self, name: str) -> bool:
        path = self.path_of(name)
        if path.exists():
            path.unlink()
            return True
        return False

    def rename(self, old: str, new: str) -> Path:
        song = self.load(old)
        old_path = self.path_of(old)
        song.name = new
        path = self.save(song)
        if old_path != path and old_path.exists():
            old_path.unlink()
        return path

    # -- listing ---------------------------------------------------------- #
    def list_paths(self) -> list[Path]:
        if not self.projects.exists():
            return []
        return sorted(self.projects.glob("*.json"))

    def list(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in self.list_paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                out.append({"name": path.stem, "error": f"{type(exc).__name__}: {exc}"})
                continue
            tracks = data.get("tracks") or []
            out.append(
                {
                    "name": data.get("name", path.stem),
                    "slug": path.stem,
                    "bpm": data.get("bpm"),
                    "meter": f"{data.get('beats_per_bar', 4)}/{data.get('beat_unit', 4)}",
                    "tracks": len(tracks),
                    "notes": sum(len(t.get("notes") or []) for t in tracks),
                    "modified": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)
                    ),
                }
            )
        return out

    def info(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "projects": str(self.projects),
            "exports": str(self.exports),
            "ipc": str(self.ipc),
            "count": len(self.list_paths()),
        }
