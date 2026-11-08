"""The MCP server: one tool per edit, one workspace on disk.

Design rules that shaped this file:

* **Every tool returns ``{"ok": ...}``.**  A model driving this server needs to
  branch on success, and an exception surfaced as a protocol error is harder to
  recover from than a value it can read.
* **Nothing is guessed about the song.**  A note is ``onset``/``duration`` in
  beats, ``pitch`` as a MIDI number and one syllable of ``lyric``.  Ticks are
  the adapters' problem.
* **Engines are named, never sniffed.**  ``export(song, engines=["synthv"])``.
  VoiSona imports an ``.svp``; that is a documented choice, not a fallback.
* **Live control is advertised only where it exists.**  Only Synthesizer V has
  a scripting channel; the other three are file-in, file-out.  ``engines()``
  says which is which so the model does not invent a live path.

Run it with ``lhai-mcp`` (stdio), which is what an MCP client config wants.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from lhai.mcp import engines as engine_pkg
from lhai.mcp.engines import base
from lhai.mcp.ir import SNote, Song, Track, normalize_language, split_lyrics
from lhai.mcp.midiio import write_midi
from lhai.mcp.store import Store
from lhai.mcp import live_tools

try:  # pragma: no cover - import shape differs between mcp 1.x and 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]

__all__ = ["mcp", "main", "build_server"]

HOME = Store()

INSTRUCTIONS = (
    "Singing-engine control for CeVIO AI, VoiSona, Synthesizer V and VOCALOID.\n"
    "Time is in beats (1 beat = one quarter note); pitch is a MIDI number; one "
    "syllable of lyric per note.\n"
    "Two layers, pick by intent:\n"
    "* offline - project_create -> notes_add -> lyrics_fill -> export -> open_in. "
    "Writes a native project for each engine and opens it.\n"
    "* live - the editor is already open: live_status shows what is running and "
    "which channel it answers on. live_* drives windows/keys/UIA and the injected "
    "agent DLL; cevio_* is CeVIO's COM talk API; synthv_live_* edits the open "
    "Synthesizer V project through its Lua agent. live_inject loads the agent "
    "for memory-level access where the app has no scripting API."
)


def build_server(store: Store | None = None) -> Any:
    server = _Server(
        name="lh-singing",
        title="LH Singing Engines",
        version="0.1.0",
        instructions=INSTRUCTIONS,
    )
    _register(server, store or HOME)
    return server


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _fail(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


def _register(server: Any, store: Store) -> None:  # noqa: C901 - a flat list of tools, on purpose

    live_tools.register(server, store)


    # ------------------------------------------------------------------ #
    # discovery
    # ------------------------------------------------------------------ #
    @server.tool()
    def engines() -> dict[str, Any]:
        """List the four installed engines, their exe paths and their control channel."""
        found = [install.as_dict() for install in base.find_all()]
        for entry in found:
            entry["running"] = base.is_running(entry["engine"])
            if entry["engine"] == "synthv":
                entry["bridge"] = engine_pkg.synthv.bridge_status(store.ipc)
        return _ok(engines=found, workspace=store.info())

    @server.tool()
    def voices(engine: str, folder: str = "") -> dict[str, Any]:
        """Installed singers/voicebanks for one engine (synthv, vocaloid, cevio, voisona).

        `folder` is optional: Synthesizer V discovers voices by reading project
        files, so pointing it at a folder of `.svp` files widens the search
        beyond the editor's recent list.
        """
        try:
            found = engine_pkg.voices(engine, folder)
        except KeyError as exc:
            return _fail(str(exc))
        return _ok(engine=engine, count=len(found), voices=found)

    # ------------------------------------------------------------------ #
    # projects
    # ------------------------------------------------------------------ #
    @server.tool()
    def project_create(
        name: str,
        bpm: float = 120.0,
        beats_per_bar: int = 4,
        beat_unit: int = 4,
        track_name: str = "Vocals",
        voice: str = "",
        language: str = "japanese",
    ) -> dict[str, Any]:
        """Create an empty song with one track and save it. Beats are quarter notes."""
        try:
            song = Song(
                name=name, bpm=bpm, beats_per_bar=beats_per_bar, beat_unit=beat_unit,
                tracks=[
                    Track(name=track_name, voice=voice, language=normalize_language(language))
                ],
            )
            song.validate()
            path = store.save(song)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, path=str(path), summary=song.summary())

    @server.tool()
    def project_list() -> dict[str, Any]:
        """List saved projects with their bpm, meter and note counts."""
        return _ok(projects=store.list(), workspace=store.info())

    @server.tool()
    def project_show(
        project: str, track: int | None = None, preview: bool = True
    ) -> dict[str, Any]:
        """Show a project: tracks, meter, validation issues and an ASCII piano roll."""
        try:
            song = store.load(project)
        except FileNotFoundError as exc:
            return _fail(str(exc))
        payload: dict[str, Any] = {"project": song.name, "summary": song.summary()}
        if preview:
            try:
                payload["preview"] = song.piano_roll(0 if track is None else track)
            except (IndexError, KeyError) as exc:
                payload["preview_error"] = str(exc)
        return _ok(**payload)

    @server.tool()
    def project_delete(project: str) -> dict[str, Any]:
        """Delete a saved project file."""
        return _ok(project=project, deleted=store.delete(project))

    @server.tool()
    def project_import(path: str, name: str = "") -> dict[str, Any]:
        """Import an existing .svp / .vsqx / .ccs / .mid as a new project."""
        try:
            song = engine_pkg.import_song(Path(path))
            if name:
                song.name = name
            song.meta["imported_from"] = str(path)
            saved = store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, path=str(saved), summary=song.summary())

    # ------------------------------------------------------------------ #
    # tracks
    # ------------------------------------------------------------------ #
    @server.tool()
    def track_add(
        project: str, name: str, voice: str = "", language: str = "japanese"
    ) -> dict[str, Any]:
        """Add a track (one singer) to an existing project."""
        try:
            song = store.load(project)
            song.tracks.append(
                Track(name=name, voice=voice, language=normalize_language(language))
            )
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, tracks=song.summary()["tracks"])

    @server.tool()
    def track_set(
        project: str,
        track: str,
        voice: str | None = None,
        language: str | None = None,
        gain_db: float | None = None,
        pan: float | None = None,
        muted: bool | None = None,
        solo: bool | None = None,
    ) -> dict[str, Any]:
        """Update a track's voice, language, gain, pan, mute or solo."""
        try:
            song = store.load(project)
            obj = song.track(track)
            if voice is not None:
                obj.voice = voice
            if language is not None:
                obj.language = normalize_language(language)
            if gain_db is not None:
                obj.gain_db = float(gain_db)
            if pan is not None:
                obj.pan = max(-1.0, min(1.0, float(pan)))
            if muted is not None:
                obj.muted = bool(muted)
            if solo is not None:
                obj.solo = bool(solo)
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        obj = song.track(track)
        return _ok(project=song.name, track=obj.to_dict() | {"notes": len(obj.notes)})

    @server.tool()
    def track_remove(project: str, track: str) -> dict[str, Any]:
        """Remove a track and its notes."""
        try:
            song = store.load(project)
            song.tracks.remove(song.track(track))
            if not song.tracks:
                song.tracks.append(Track(name="Track 1"))
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, tracks=song.summary()["tracks"])

    # ------------------------------------------------------------------ #
    # notes
    # ------------------------------------------------------------------ #
    @server.tool()
    def notes_add(
        project: str, track: str, notes: Sequence[dict[str, Any]], replace: bool = False
    ) -> dict[str, Any]:
        """Add notes to a track.

        Each note: {"onset": beats, "duration": beats, "pitch": midi,
        "lyric": "a"} - optional "phonemes" and "language".  `replace` clears the
        track first.
        """
        try:
            song = store.load(project)
            obj = song.track(track)
            incoming = [SNote.from_any(item) for item in notes]
            if replace:
                obj.notes = []
            obj.notes.extend(incoming)
            obj.notes.sort(key=lambda n: (n.onset, n.pitch))
            song.validate()
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(
            project=song.name,
            track=obj.name,
            added=len(incoming),
            total=len(obj.notes),
            beats=round(obj.end_beat, 3),
        )

    @server.tool()
    def notes_list(project: str, track: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        """List a track's notes in time order (paged)."""
        try:
            song = store.load(project)
            obj = song.track(track)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        notes = obj.sorted_notes()
        start = max(0, offset)
        window = notes[start : start + max(1, min(500, limit))]
        return _ok(
            project=song.name,
            track=obj.name,
            total=len(notes),
            offset=offset,
            notes=[{**n.to_dict(), "index": start + i} for i, n in enumerate(window)],
        )

    @server.tool()
    def note_update(
        project: str,
        track: str,
        index: int,
        onset: float | None = None,
        duration: float | None = None,
        pitch: int | None = None,
        lyric: str | None = None,
        phonemes: str | None = None,
    ) -> dict[str, Any]:
        """Edit one note by index (see notes_list for indices)."""
        try:
            song = store.load(project)
            obj = song.track(track)
            ordered = obj.sorted_notes()
            note = ordered[index]
            if onset is not None:
                note.onset = float(onset)
            if duration is not None:
                note.duration = float(duration)
            if pitch is not None:
                note.pitch = int(pitch)
            if lyric is not None:
                note.lyric = lyric
            if phonemes is not None:
                note.phonemes = phonemes
            obj.notes.sort(key=lambda n: (n.onset, n.pitch))
            song.validate()
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, note=note.to_dict())

    @server.tool()
    def note_delete(project: str, track: str, indexes: Sequence[int]) -> dict[str, Any]:
        """Delete notes by index (indices are drop-safe: highest first internally)."""
        try:
            song = store.load(project)
            obj = song.track(track)
            ordered = obj.sorted_notes()
            # Identity, not equality: SNote is a mutable dataclass (unhashable),
            # and two distinct notes can carry identical values.
            doomed = {id(ordered[i]) for i in indexes if -len(ordered) <= i < len(ordered)}
            obj.notes = [n for n in obj.notes if id(n) not in doomed]
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, remaining=len(obj.notes))

    @server.tool()
    def notes_clear(project: str, track: str) -> dict[str, Any]:
        """Remove every note from a track (keeps the track and its voice)."""
        try:
            song = store.load(project)
            obj = song.track(track)
            cleared = len(obj.notes)
            obj.notes = []
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, cleared=cleared)

    # ------------------------------------------------------------------ #
    # musical operations
    # ------------------------------------------------------------------ #
    @server.tool()
    def lyrics_fill(
        project: str, track: str, text: str, start_index: int = 0, mode: str = "auto"
    ) -> dict[str, Any]:
        """Assign a lyric line to existing notes, one syllable per note.

        Split rules: '-' and '|' are explicit separators, whitespace separates
        words, an unspaced CJK string splits per character, and a Latin word
        stays whole (`mode="auto"`) or splits on vowel groups
        (`mode="syllables"`).  Unused syllables and notes left without a lyric
        are reported, never dropped silently.
        """
        try:
            song = store.load(project)
            obj = song.track(track)
            syllables = split_lyrics(text, latin="syllables" if mode == "syllables" else "auto")
            ordered = obj.sorted_notes()
            assigned = 0
            for offset, syllable in enumerate(syllables):
                position = start_index + offset
                if position >= len(ordered):
                    break
                ordered[position].lyric = syllable
                assigned += 1
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        leftover = max(0, len(syllables) - assigned)
        return _ok(
            project=song.name,
            track=obj.name,
            syllables=len(syllables),
            assigned=assigned,
            notes=len(obj.notes),
            unused_syllables=syllables[assigned:] if leftover else [],
            notes_without_lyric=max(0, len(obj.notes) - start_index - assigned),
        )

    @server.tool()
    def transpose(project: str, track: str, semitones: int, start_index: int = 0) -> dict[str, Any]:
        """Shift pitch by semitones (clamped to 0..127)."""
        try:
            song = store.load(project)
            obj = song.track(track)
            ordered = obj.sorted_notes()
            for note in ordered[start_index:]:
                if note.pitch >= 0:
                    note.pitch = max(0, min(127, note.pitch + int(semitones)))
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, semitones=semitones)

    @server.tool()
    def quantize(
        project: str, track: str, grid: float = 0.25, durations: bool = True
    ) -> dict[str, Any]:
        """Snap note onsets (and durations unless `durations=False`) to a beat grid."""
        if grid <= 0:
            return _fail("grid must be > 0")
        try:
            song = store.load(project)
            obj = song.track(track)
            for note in obj.notes:
                note.onset = round(round(note.onset / grid) * grid, 6)
                if durations:
                    note.duration = max(grid, round(round(note.duration / grid) * grid, 6))
            obj.notes.sort(key=lambda n: (n.onset, n.pitch))
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, grid=grid)

    @server.tool()
    def shift(project: str, track: str, beats: float, start_index: int = 0) -> dict[str, Any]:
        """Move notes in time by `beats` (negative moves earlier)."""
        try:
            song = store.load(project)
            obj = song.track(track)
            ordered = obj.sorted_notes()
            for note in ordered[start_index:]:
                note.onset = max(0.0, round(note.onset + float(beats), 6))
            obj.notes.sort(key=lambda n: (n.onset, n.pitch))
            store.save(song)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, track=obj.name, beats=beats)

    # ------------------------------------------------------------------ #
    # export / open / live
    # ------------------------------------------------------------------ #
    @server.tool()
    def export(
        project: str,
        engines: Sequence[str] | None = None,
        filename: str = "",
        voice_id: str = "",
    ) -> dict[str, Any]:
        """Write the project in each engine's native format.

        Defaults to all four.  `voice_id` only affects VOCALOID (16-char
        component id; omit to let the editor pick its default singer).
        """
        try:
            song = store.load(project)
        except FileNotFoundError as exc:
            return _fail(str(exc))
        wanted = list(engines) if engines else list(engine_pkg.ENGINE_KEYS)
        unknown = [e for e in wanted if e not in engine_pkg.ENGINE_KEYS]
        if unknown:
            return _fail(f"unknown engine(s) {unknown}; expected {list(engine_pkg.ENGINE_KEYS)}")
        written: list[dict[str, Any]] = []
        errors: list[str] = []
        for engine in wanted:
            try:
                written.append(
                    engine_pkg.export_song(
                        song,
                        engine,
                        store.exports,
                        filename=filename or None,
                        voice_id=voice_id or None,
                        voice_name=song.tracks[0].voice if song.tracks else "",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one engine failing must not stop the rest
                errors.append(f"{engine}: {type(exc).__name__}: {exc}")
        return _ok(project=song.name, files=written, errors=errors)

    @server.tool()
    def export_midi(project: str, filename: str = "") -> dict[str, Any]:
        """Write a Standard MIDI File - the one format all four engines import."""
        try:
            song = store.load(project)
            path = store.exports / f"{filename or song.name}.mid"
            write_midi(song, path)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(project=song.name, path=str(path))

    @server.tool()
    def open_in(project: str, engine: str, fmt: str = "") -> dict[str, Any]:
        """Export for `engine` and launch it with that file.

        `fmt` picks the file the engine receives (e.g. "mid" for an engine whose
        native writer you want to bypass).
        """
        try:
            song = store.load(project)
            if fmt:
                suffix = fmt if fmt.startswith(".") else f".{fmt}"
                if suffix == ".mid":
                    path = store.exports / f"{song.name}{suffix}"
                    write_midi(song, path)
                    result = {"engine": engine, "path": str(path), "format": suffix}
                else:
                    result = engine_pkg.export_song(
                        song, engine, store.exports, fmt=suffix,
                        voice_name=song.tracks[0].voice if song.tracks else "",
                    )
            else:
                result = engine_pkg.export_song(
                    song, engine, store.exports,
                    voice_name=song.tracks[0].voice if song.tracks else "",
                )
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        launch = base.launch(engine, result["path"])
        return _ok(**result, launch=launch)

    @server.tool()
    def synthv_bridge_install() -> dict[str, Any]:
        """Install/refresh the resident Lua agent in Synthesizer V's script folder."""
        try:
            store.ensure()
            result = engine_pkg.synthv.install_bridge(store.ipc)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(**result)

    @server.tool()
    def synthv_status() -> dict[str, Any]:
        """Report whether the SynthV Lua bridge is installed, current and answerable."""
        return _ok(
            bridge=engine_pkg.synthv.bridge_status(store.ipc),
            engine=base.find_engine("synthv").as_dict(),
            running=base.is_running("synthv"),
        )

    @server.tool()
    def synthv_push(
        project: str, track: int = 0, clear: bool = True, timeout: float = 15.0
    ) -> dict[str, Any]:
        """Push a track's notes into the running Synthesizer V via the Lua bridge.

        Requires the bridge script to be running (Scripts > LHai Agent).  Edits
        happen in the group selected by the agent; `clear` empties it first.
        """
        try:
            store.ensure()
            song = store.load(project)
            result = engine_pkg.synthv.push_to_synthv(
                song, track=track, ipc_dir=store.ipc, clear=clear, timeout=timeout
            )
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        return _ok(**result) if result.get("pushed") else _fail(
            result.get("reason", "push failed"), **result
        )

    @server.tool()
    def song_validate(project: str) -> dict[str, Any]:
        """Check a project for schema problems and overlapping notes."""
        try:
            song = store.load(project)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        issues = song.validate(strict=False)
        return _ok(project=song.name, issues=issues, overlaps=song.overlaps(), clean=not issues)


mcp = build_server()


def main(argv: Sequence[str] | None = None) -> int:
    """stdio entry point.  ``lhai-mcp --help`` prints the transport options."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    transport = "stdio"
    if args and args[0] in ("--http", "http", "--sse", "sse"):
        transport = "streamable-http" if args[0].lstrip("-") == "http" else "sse"
    if args and args[0] in ("-h", "--help"):
        print(
            "lhai-mcp [stdio|http|sse]\n"
            "  stdio (default): what MCP clients launch\n"
            "  http           : streamable HTTP on MCP_HOST:MCP_PORT/mcp"
            " (default 127.0.0.1:8000)\n"
            "  sse            : legacy SSE transport on MCP_HOST:MCP_PORT/sse\n"
            "Workspace: $LH_MCP_HOME or %LOCALAPPDATA%/lh-ai-studio/singing"
        )
        return 0
    if transport == "stdio":
        mcp.run(transport="stdio")
        return 0
    # The HTTP transports bind a socket, so they are the only ones that take an
    # address.  Without this the SDK's host/port defaults are the only option and
    # two MCP servers on one machine fight over 8000.
    import os

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("MCP_PORT", "8000"))
    except ValueError:
        port = 8000
    mcp.run(transport=transport, host=host, port=port)  # type: ignore[arg-type]
    return 0
