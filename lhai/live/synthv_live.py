"""Client for the resident SynthV Lua agent: push a score, read it back, drive transport.

The agent lives in the editor and writes lines to a result file; this module is
the half that owns the timing.  Two details make it reliable in practice:

* the command file is written to a temp name and then :func:`os.replace`-d, so
  the agent can never read a half-written batch;
* the result file is read from the offset it had *before* the batch was queued,
  so an answer from an earlier call can never be mistaken for this one's.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from lhai.live import synthv_lua

__all__ = [
    "BLICK_PER_QUARTER",
    "live_dir",
    "send",
    "state",
    "tracks",
    "probe",
    "version",
    "push_notes",
    "install",
    "status",
]

#: SynthV measures time in "blicks"; one quarter note is this many.
BLICK_PER_QUARTER = 705_600_000


def _running_exes() -> list[str]:
    """Executable paths of every running Synthesizer V editor, either generation."""
    try:
        from lhai.live import apps, win32
    except Exception:  # pragma: no cover - import guard, never fatal here
        return []
    paths: list[str] = []
    for pid in win32.pids_of_images(apps.IMAGES["synthv"]):
        path = win32.image_path(pid)
        if path:
            paths.append(path)
    return paths


def _running_exe() -> str:
    """Path of a running editor's executable, or ``""``.

    The image name is identical for both generations of Synthesizer V, so the
    process has to be asked where it was launched from.  With both up this is
    just the first one the OS lists, which is why :func:`live_dir` does not
    simply trust it.
    """
    paths = _running_exes()
    return paths[0] if paths else ""


def running_editions() -> list[dict[str, str]]:
    """Every running editor and the generation it belongs to.

    Both generations can be up at once with separate command folders, so a caller
    that wants one of them has to be able to say which.  This is how it finds out
    there was a choice.
    """
    return [
        {"edition": synthv_lua.edition_of(exe) or "unknown", "exe": exe}
        for exe in _running_exes()
    ]


def live_dir(ipc_dir: str | Path, edition: str = "") -> Path:
    """The command folder to talk to.

    Each generation's agent owns its own command/result pair, so one shared file
    would let two agents claim the same batch and apply it twice.  ``edition``
    names the generation wanted.  Without it the newest *running* generation wins:
    with both up, "whichever process the OS happened to list first" is not a
    decision, and a batch landing in the other editor is the kind of bug that
    looks like a broken agent.  With no editor up this falls back to the newest
    installed generation, which keeps ``install`` and ``status`` meaningful on an
    idle machine.
    """
    if edition:
        return synthv_lua.live_ipc(ipc_dir, edition=edition)
    running = _running_exes()
    for key, _name in synthv_lua.EDITIONS:
        if any(synthv_lua.edition_of(exe) == key for exe in running):
            return synthv_lua.live_ipc(ipc_dir, edition=key)
    return synthv_lua.live_ipc(ipc_dir, exe=running[0] if running else "")


def _paths(ipc_dir: str | Path) -> tuple[Path, Path]:
    ipc = Path(ipc_dir)
    return ipc / "synthv_cmd.txt", ipc / "synthv_result.txt"


def install(ipc_dir: str | Path) -> dict[str, Any]:
    return synthv_lua.install(ipc_dir)


def status(ipc_dir: str | Path) -> dict[str, Any]:
    return synthv_lua.status(ipc_dir)


def send(
    ipc_dir: str | Path,
    commands: Sequence[str],
    timeout: float = 15.0,
    poll: float = 0.2,
    edition: str = "",
) -> dict[str, Any]:
    """Queue one batch and wait for the agent's ``RESULT`` line.

    ``edition`` picks the generation (``"sv1"`` / ``"sv2"``) when both are up;
    see :func:`live_dir`.
    """
    ipc = live_dir(ipc_dir, edition=edition)
    ipc.mkdir(parents=True, exist_ok=True)
    cmd_file, res_file = _paths(ipc)

    token = uuid.uuid4().hex[:12]
    payload = "\n".join([*commands, f"END {token}"]) + "\n"

    try:
        offset = res_file.stat().st_size
    except OSError:
        offset = 0

    tmp = cmd_file.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, cmd_file)

    deadline = time.time() + max(0.5, float(timeout))
    marker = f"RESULT {token}"
    text = ""
    while time.time() < deadline:
        try:
            with res_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                text = handle.read()
        except OSError:
            text = ""
        if marker in text:
            break
        time.sleep(poll)

    if marker not in text:
        return {
            "ok": False,
            "error": (
                "the Lua agent did not answer - run Scripts > LHai Agent in "
                "Synthesizer V once per session"
            ),
            "ipc_dir": str(ipc),
            "bridge": status(ipc_dir),
            "commands": list(commands),
        }

    head, _, tail = text.partition(marker)
    results = _parse_results(head)
    return {
        "ok": True,
        "token": token,
        "ipc_dir": str(ipc),
        "results": results,
        "failed": [r for r in results if not r["ok"]],
        "summary": tail.strip(),
        "channel": "lua",
    }


def _parse_results(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("R "):
            continue
        body = line[2:]
        verb, _, rest = body.partition(" ")
        status, _, payload = rest.partition(" ")
        out.append({"verb": verb, "ok": status == "ok", "payload": payload.strip()})
    return out


def state(ipc_dir: str | Path, group: int = 1, timeout: float = 10.0) -> dict[str, Any]:
    """Read the current track's notes back out of the editor."""
    result = send(ipc_dir, [f"GROUP {int(group)}", "STATE"], timeout=timeout)
    if not result.get("ok"):
        return result
    payload = next(
        (r["payload"] for r in result["results"] if r["verb"] == "STATE" and r["ok"]), ""
    )
    notes = []
    for chunk in payload.split("|"):
        parts = chunk.split(" ", 5)
        if len(parts) < 5 or parts[0] != "N":
            continue
        try:
            notes.append(
                {
                    "index": int(parts[1]),
                    "onset": int(parts[2]) / BLICK_PER_QUARTER,
                    "duration": int(parts[3]) / BLICK_PER_QUARTER,
                    "pitch": int(parts[4]),
                    "lyric": parts[5] if len(parts) > 5 else "",
                }
            )
        except ValueError:
            continue
    return {"ok": True, "notes": notes, "count": len(notes), "channel": "lua", "results": result["results"]}


def tracks(ipc_dir: str | Path, timeout: float = 10.0) -> dict[str, Any]:
    result = send(ipc_dir, ["TRACKS"], timeout=timeout)
    if not result.get("ok"):
        return result
    payload = next(
        (r["payload"] for r in result["results"] if r["verb"] == "TRACKS" and r["ok"]), ""
    )
    entries = []
    for chunk in payload.split("|"):
        parts = chunk.split(" ", 3)
        if len(parts) < 4 or parts[0] != "T":
            continue
        try:
            entries.append({"index": int(parts[1]), "groups": int(parts[2]), "name": parts[3]})
        except ValueError:
            continue
    return {"ok": True, "tracks": entries, "channel": "lua"}


def probe(ipc_dir: str | Path, timeout: float = 10.0) -> dict[str, Any]:
    """Which API calls this editor build actually supports."""
    result = send(ipc_dir, ["PROBE"], timeout=timeout)
    if not result.get("ok"):
        return result
    payload = next(
        (r["payload"] for r in result["results"] if r["verb"] == "PROBE" and r["ok"]), ""
    )
    return {"ok": True, "supported": [s for s in payload.split(",") if s], "channel": "lua"}


def version(ipc_dir: str | Path, timeout: float = 10.0) -> dict[str, Any]:
    """Which host the agent is running inside, straight from the editor.

    This is how a caller confirms *which* generation answered instead of
    inferring it from an install folder - useful precisely because both
    generations share an executable name.
    """
    result = send(ipc_dir, ["VERSION"], timeout=timeout)
    if not result.get("ok"):
        return result
    payload = next(
        (r["payload"] for r in result["results"] if r["verb"] == "VERSION" and r["ok"]), ""
    )
    if not payload:
        failed = next(
            (r["payload"] for r in result["results"] if r["verb"] == "VERSION" and not r["ok"]),
            "no answer",
        )
        return {"ok": False, "error": failed, "channel": "lua"}
    fields: dict[str, Any] = {}
    for chunk in payload.split("|"):
        key, _, value = chunk.partition("=")
        if key:
            fields[key.strip()] = value.strip()
    fields["ok"] = True
    fields["channel"] = "lua"
    fields["edition"] = synthv_lua.edition_of(_running_exe()) or "unknown"
    return fields


def push_notes(
    ipc_dir: str | Path,
    notes: Iterable[dict[str, Any]],
    *,
    group: int = 1,
    clear: bool = True,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Replace a note group with ``notes`` (beats in, blicks out)."""
    commands = [f"GROUP {int(group)}"]
    if clear:
        commands.append("CLEAR")
    queued = 0
    for note in notes:
        onset = int(round(float(note.get("onset", 0.0)) * BLICK_PER_QUARTER))
        duration = max(1, int(round(float(note.get("duration", 1.0)) * BLICK_PER_QUARTER)))
        pitch = int(note.get("pitch", 60))
        lyric = str(note.get("lyric", "a") or "a").replace(" ", "_")
        language = str(note.get("language", "") or "-")
        commands.append(f"NOTE {onset} {duration} {pitch} {lyric} {language}")
        queued += 1
    result = send(ipc_dir, commands, timeout=timeout)
    if result.get("ok"):
        result["queued"] = queued
        result["cleared"] = bool(clear)
    return result
