"""MCP tools that touch a *running* editor.

Every tool here answers the same question the file-based tools cannot: "do it in
the app I already have open".  They are thin on purpose - the policy (which
channel, what to fall back to) lives in :mod:`lhai.live`, and a tool that cannot
act says why instead of returning a cheerful ``ok``.

Naming: ``live_*`` for the cross-engine surface, ``song_*`` for the song
(singing synthesis) surface, ``cevio_*`` for the COM talk API, ``synthv_live_*``
for the Lua agent.
"""

from __future__ import annotations

from typing import Any, Sequence

from lhai.live import agent as agent_mod
from lhai.live import apps, bridges, cevio_com, inject, song, synthv_live, synthv_lua, uia, win32

__all__ = ["register"]


def _ok(**payload: Any) -> dict[str, Any]:
    return {"ok": True, **payload}


def _fail(message: str, **payload: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **payload}


def register(server: Any, store: Any) -> None:  # noqa: C901 - a flat list of tools, on purpose

    def _engine(key: str) -> tuple[apps.LiveEngine | None, dict[str, Any] | None]:
        try:
            live = apps.find(key)
        except KeyError as exc:
            return None, _fail(str(exc))
        if not live.running:
            return None, _fail(
                f"{live.label} is not running",
                engine=key,
                hint=f"live_launch(engine='{key}') starts it",
            )
        return live, None

    # ------------------------------------------------------------------ #
    # discovery / lifecycle
    # ------------------------------------------------------------------ #
    @server.tool()
    def live_status() -> dict[str, Any]:
        """Which of the four editors are running, their windows, and their live channels."""
        payload = apps.status()
        payload["uia"] = uia.available()
        payload["agent_dll"] = str(inject.agent_dll_path())
        payload["agent_built"] = inject.agent_dll_path().exists()
        payload["synthv_bridge"] = synthv_live.status(store.ipc)
        return _ok(**payload)

    @server.tool()
    def live_launch(engine: str, file: str = "") -> dict[str, Any]:
        """Start an editor (cevio, voisona, synthv, vocaloid) and wait for its window."""
        return apps.launch(engine, file)

    @server.tool()
    def live_focus(engine: str) -> dict[str, Any]:
        """Bring an editor's main window to the foreground."""
        return apps.focus(engine)

    @server.tool()
    def live_close(engine: str, force: bool = False) -> dict[str, Any]:
        """Close an editor politely (WM_CLOSE), or kill it with `force`."""
        return apps.close(engine, force=force)

    @server.tool()
    def live_windows(engine: str = "") -> dict[str, Any]:
        """Top-level windows, for one engine or every window on the desktop."""
        if engine:
            live = apps.find(engine)
            pids = {live.pid} if live.pid else set()
        else:
            pids = set()
        found = [
            w.as_dict()
            for w in win32.list_windows(visible_only=False)
            if (not pids or w.pid in pids) and w.title
        ]
        return _ok(count=len(found), windows=found)

    # ------------------------------------------------------------------ #
    # UI Automation
    # ------------------------------------------------------------------ #
    @server.tool()
    def live_uia_tree(engine: str, depth: int = 5, raw: bool = False) -> dict[str, Any]:
        """The control tree of an editor window (names, roles, ids, rectangles)."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        tree = uia.dump_tree(live.hwnd, depth=max(1, min(12, int(depth))), raw=raw)
        if tree is None:
            return _fail("window has no automation element", engine=engine, hwnd=live.hwnd)
        return _ok(engine=engine, hwnd=live.hwnd, tree=tree.as_dict())

    @server.tool()
    def live_uia_find(
        engine: str, name: str = "", role: str = "", automation_id: str = "", limit: int = 10
    ) -> dict[str, Any]:
        """Find controls by (substring) name, control type, or automation id."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        hits = uia.find_all(
            live.hwnd, name=name, role=role, automation_id=automation_id, limit=max(1, min(50, limit))
        )
        return _ok(
            engine=engine,
            count=len(hits),
            matches=[uia.describe(hit).as_dict(with_children=False) for hit in hits],
        )

    @server.tool()
    def live_uia_click(
        engine: str,
        name: str = "",
        role: str = "",
        automation_id: str = "",
        x: int | None = None,
        y: int | None = None,
        double: bool = False,
    ) -> dict[str, Any]:
        """Press a control by name, or click raw screen coordinates."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        if x is not None and y is not None:
            win32.focus(live.hwnd)
            win32.click_at(int(x), int(y), clicks=2 if double else 1)
            return _ok(engine=engine, via="mouse", at=[x, y])
        element = uia.find(live.hwnd, name=name, role=role, automation_id=automation_id)
        if element is None:
            return _fail("no control matched", engine=engine, name=name, role=role)
        if double:
            result = uia.click(element)
            if result.get("ok"):
                at = result.get("at") or [0, 0]
                win32.click_at(int(at[0]), int(at[1]))
            return result
        result = uia.invoke(element)
        result.update({"engine": engine, "name": uia.describe(element).name})
        return result

    @server.tool()
    def live_uia_set_text(
        engine: str, text: str, name: str = "", automation_id: str = ""
    ) -> dict[str, Any]:
        """Set a text field's value through the Value pattern."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        element = uia.find(live.hwnd, name=name, automation_id=automation_id)
        if element is None:
            return _fail("no text field matched", engine=engine, name=name)
        result = uia.set_text(element, text)
        result["engine"] = engine
        return result

    # ------------------------------------------------------------------ #
    # keyboard
    # ------------------------------------------------------------------ #
    @server.tool()
    def live_keys(engine: str, keys: Sequence[str] | str) -> dict[str, Any]:
        """Send a chord ("ctrl+s") or a key sequence (["enter", "tab"]) to an editor."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        focused = win32.focus(live.hwnd)
        if isinstance(keys, str):
            sent = win32.hotkey(keys) if any(sep in keys for sep in "+-") else win32.press([keys])
            description = keys
        else:
            sent = win32.send_keys(keys)
            description = "+".join(str(k) for k in keys)
        return _ok(engine=engine, focused=focused, sent=sent, keys=description)

    @server.tool()
    def live_type(engine: str, text: str) -> dict[str, Any]:
        """Type text into the focused editor (Unicode, no clipboard)."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        focused = win32.focus(live.hwnd)
        return _ok(engine=engine, focused=focused, typed=win32.type_text(text), length=len(text))

    # ------------------------------------------------------------------ #
    # injected agent
    # ------------------------------------------------------------------ #
    @server.tool()
    def live_inject(engine: str) -> dict[str, Any]:
        """Load the agent DLL into an editor and wait for its pipe to answer."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        result = agent_mod.ensure(live.pid, auto_inject=True)
        return _ok(engine=engine, pid=live.pid, **result) if result.get("ok") else _fail(
            result.get("error", "injection failed"), engine=engine, pid=live.pid
        )

    @server.tool()
    def live_eject(engine: str) -> dict[str, Any]:
        """Unload the agent DLL from an editor."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        return inject.eject(live.pid)

    @server.tool()
    def live_agent(engine: str, command: str, args: Sequence[str] = ()) -> dict[str, Any]:
        """Raw agent command (PING, MODULES, READ, WRITE, SCAN, KEY, HOTKEY, TYPE, FOCUS, WINDOWS, CLIPGET/CLIPSET)."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        ready = agent_mod.ensure(live.pid, auto_inject=True)
        if not ready.get("ok"):
            return _fail(ready.get("error", "agent unavailable"), engine=engine, pid=live.pid)
        return agent_mod.call(live.pid, command.upper(), *[str(a) for a in args])

    @server.tool()
    def live_mem_scan(engine: str, pattern: str, module: str = "") -> dict[str, Any]:
        """Signature-scan a module inside the editor; pattern like "48 8B ?? ??"."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        ready = agent_mod.ensure(live.pid, auto_inject=True)
        if not ready.get("ok"):
            return _fail(ready.get("error", "agent unavailable"), engine=engine)
        result = agent_mod.scan(live.pid, pattern, module)
        if result.get("ok"):
            result["hits_hex"] = [hex(hit) for hit in result.get("hits", [])]
        return result

    @server.tool()
    def live_mem_read(engine: str, address: str, size: int = 64) -> dict[str, Any]:
        """Read `size` bytes at a hex address inside the editor."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        ready = agent_mod.ensure(live.pid, auto_inject=True)
        if not ready.get("ok"):
            return _fail(ready.get("error", "agent unavailable"), engine=engine)
        try:
            target = int(str(address), 16)
        except ValueError:
            return _fail(f"address must be hex, got {address!r}")
        result = agent_mod.read_mem(live.pid, target, max(1, min(1 << 20, int(size))))
        if result.get("ok") and "data" in result:
            result["hex"] = result.pop("data").hex()
        return result

    # ------------------------------------------------------------------ #
    # CeVIO talk (COM)
    # ------------------------------------------------------------------ #
    @server.tool()
    def cevio_casts() -> dict[str, Any]:
        """List CeVIO's installed talk casts (COM remote service)."""
        return cevio_com.casts()

    @server.tool()
    def cevio_say(text: str, cast: str = "", wait: bool = False) -> dict[str, Any]:
        """Speak text through CeVIO AI's talk voice."""
        return cevio_com.say(text, cast=cast, wait=wait)

    @server.tool()
    def cevio_wave(text: str, path: str, cast: str = "") -> dict[str, Any]:
        """Render talk to a WAV file with CeVIO AI."""
        return cevio_com.wave(text, path, cast=cast)

    @server.tool()
    def cevio_voice(
        cast: str = "",
        component: str = "",
        component_value: int | None = None,
        volume: int | None = None,
        speed: int | None = None,
        tone: int | None = None,
        tone_scale: int | None = None,
        alpha: int | None = None,
    ) -> dict[str, Any]:
        """Set CeVIO talk cast, a style component's value, and the five sliders (0-100)."""
        return cevio_com.set_voice(
            cast=cast,
            component=component,
            component_value=component_value,
            volume=volume,
            speed=speed,
            tone=tone,
            tone_scale=tone_scale,
            alpha=alpha,
        )

    @server.tool()
    def cevio_state() -> dict[str, Any]:
        """Current CeVIO talk cast, style components and slider values."""
        return cevio_com.state()

    @server.tool()
    def cevio_phonemes(text: str) -> dict[str, Any]:
        """The phoneme sequence and timing CeVIO would synthesise for a line."""
        return cevio_com.phonemes(text)

    # ------------------------------------------------------------------ #
    # Synthesizer V live (Lua)
    # ------------------------------------------------------------------ #
    @server.tool()
    def synthv_live_install() -> dict[str, Any]:
        """Install/refresh the resident Lua agent in Synthesizer V's script folder."""
        store.ensure()
        return _ok(**synthv_live.install(store.ipc))

    @server.tool()
    def synthv_live_status() -> dict[str, Any]:
        """Bridge install state per Synthesizer V generation, plus whether one is running.

        Both ``Synthesizer V Studio`` (1.x) and ``Synthesizer V Studio 2`` are
        supported; each gets its own script and its own command/result folder, so
        ``edition`` here is which one the client would talk to right now.
        """
        live = apps.find("synthv")
        exe = win32.image_path(live.pid) if live.pid else live.exe
        return _ok(
            bridge=synthv_live.status(store.ipc),
            running=live.running,
            pid=live.pid,
            exe=exe,
            edition=synthv_lua.edition_of(exe) or "unknown",
            running_editions=synthv_live.running_editions(),
            live_ipc_dir=str(synthv_live.live_dir(store.ipc)),
        )

    @server.tool()
    def synthv_live_cmd(commands: Sequence[str], edition: str = "") -> dict[str, Any]:
        """Send raw commands to the Lua agent (GROUP/CLEAR/NOTE/STATE/LYRIC/DEL/PHONEME/PITCH/TIMESHIFT/PLAY/STOP/SEEK/UNDO/REDO/SAVE/PROBE/VERSION).

        Both generations of Synthesizer V can run at once and each has its own
        command folder, so pass ``edition`` (``"sv1"`` / ``"sv2"``) to choose;
        without it the newest running one is used. ``synthv_live_status`` lists
        what is up.
        """
        store.ensure()
        return synthv_live.send(store.ipc, list(commands), edition=edition)

    @server.tool()
    def synthv_live_state(group: int = 1) -> dict[str, Any]:
        """Read the open project's current-track notes back out of Synthesizer V."""
        store.ensure()
        return synthv_live.state(store.ipc, group=group)

    @server.tool()
    def synthv_live_push(project: str, track: int = 0, clear: bool = True, group: int = 1) -> dict[str, Any]:
        """Push a saved project's track into the running Synthesizer V, live."""
        store.ensure()
        try:
            song = store.load(project)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"{type(exc).__name__}: {exc}")
        if not song.tracks:
            return _fail("project has no tracks")
        index = max(0, min(int(track), len(song.tracks) - 1))
        notes = [
            {
                "onset": note.onset,
                "duration": note.duration,
                "pitch": note.pitch,
                "lyric": note.lyric,
                "language": note.language or song.tracks[index].language,
            }
            for note in song.tracks[index].sorted_notes()
            if note.pitch >= 0
        ]
        result = synthv_live.push_notes(store.ipc, notes, group=group, clear=clear)
        result["track"] = song.tracks[index].name
        return result

    @server.tool()
    def synthv_live_probe() -> dict[str, Any]:
        """Which SynthV Lua API calls this editor build actually supports."""
        store.ensure()
        return synthv_live.probe(store.ipc)

    @server.tool()
    def synthv_live_version() -> dict[str, Any]:
        """Which host the Lua agent is running in (name, version, OS, UI language).

        This is the honest answer to "am I talking to 1.x or 2.x": both
        generations ship the same executable name, so the editor itself is asked.
        """
        store.ensure()
        return synthv_live.version(store.ipc)

    # ------------------------------------------------------------------ #
    # the song itself (singing synthesis, not talk)
    # ------------------------------------------------------------------ #
    @server.tool()
    def song_caps(engine: str = "") -> dict[str, Any]:
        """What each editor can change in a *running* song, and through which channel.

        One engine or all four.  The answer differs sharply per engine - a real
        in-process API on Synthesizer V, UI Automation everywhere else - so this
        is the tool to call before promising a caller anything.
        """
        return _ok(capabilities=song.caps(engine), engines=list(song.ENGINES))

    @server.tool()
    def song_learn(engine: str, save: bool = True) -> dict[str, Any]:
        """Read a running editor's toolbar and record which control is which.

        CeVIO's control map ships.  VOCALOID6 and VoiSona change their ids
        between builds, so their map is *measured* once and reused - a guessed id
        clicks something else.
        """
        return song.learn(engine, save=save)

    @server.tool()
    def song_map(engine: str, reload: bool = False) -> dict[str, Any]:
        """The control map in use for an engine: name -> automation id."""
        return _ok(engine=engine, map=song.control_map(engine, reload=reload))

    @server.tool()
    def song_transport(engine: str, action: str) -> dict[str, Any]:
        """Start or stop playback: play, stop, head, repeat, marker_start, marker_end.

        The new state is read back, so `active` is evidence rather than a guess.
        """
        return song.transport(engine, action)

    @server.tool()
    def song_tool(engine: str, name: str) -> dict[str, Any]:
        """Select the edit tool: arrow, pen, line, eraser, bulk."""
        return song.tool(engine, name)

    @server.tool()
    def song_lane(engine: str, name: str) -> dict[str, Any]:
        """Switch the visible parameter lane: volume, pitch, vibrato, alpha, husky, timing.

        These six curves are what a vocal is shaped with, and on CeVIO they are
        disabled until a song track exists - which is reported, not swallowed.
        """
        return song.lane(engine, name)

    @server.tool()
    def song_modes(engine: str) -> dict[str, Any]:
        """Which tool, lane and transport mode are currently active in the editor."""
        return song.modes(engine)

    @server.tool()
    def song_readouts(engine: str) -> dict[str, Any]:
        """Tempo, time signature, position and quantize as shown on the toolbar."""
        return song.readouts(engine)

    @server.tool()
    def song_lyrics(engine: str, text: str, phonemes: bool = False) -> dict[str, Any]:
        """Fill the selected notes with a lyric block, without leaving the editor.

        One syllable per note, in order.  CeVIO uses its bulk lyric dialog (F8,
        or Ctrl+P with phonemes); VOCALOID6 and VoiSona take a clipboard paste.
        """
        return song.lyrics(engine, text, phonemes=phonemes)

    @server.tool()
    def song_snapshot(engine: str, path: str = "") -> dict[str, Any]:
        """Read the *open* song out of the editor and parse it.

        The score is not a control on three of the four engines, so this uses the
        editor's own save and our own reader - nothing is transcribed by hand.
        Pass `path` when the project has never been saved.
        """
        return song.snapshot(engine, path=path)

    @server.tool()
    def song_open(engine: str, path: str) -> dict[str, Any]:
        """Open a project file in the running editor (Ctrl+O)."""
        return song.open_song(engine, path)

    @server.tool()
    def song_undo_redo(engine: str, redo: bool = False) -> dict[str, Any]:
        """Undo, or redo, on the editor's own stack - one call, no bookkeeping."""
        return song.redo(engine) if redo else song.undo(engine)

    @server.tool()
    def song_save(engine: str) -> dict[str, Any]:
        """Save the open project in place (Ctrl+S)."""
        return song.save(engine)

    # ------------------------------------------------------------------ #
    # in-process bridges (bridges/)
    # ------------------------------------------------------------------ #
    @server.tool()
    def bridge_list(engine: str = "") -> dict[str, Any]:
        """In-process bridges answering right now, and which engine each belongs to.

        A bridge is a small assembly loaded *inside* an editor's own process; it
        holds that editor's object model and publishes MCP on loopback HTTP.  That
        is the real path - no keyboard, no mouse, no screen.  The ports are not
        configured; they are discovered by knocking, so this is the tool to call
        before any ``bridge_*`` call.
        """
        found = bridges.discover(engine)
        return _ok(count=len(found), bridges=found, port_window=[bridges.PORT_FIRST, bridges.PORT_LAST])

    @server.tool()
    def bridge_status(engine: str) -> dict[str, Any]:
        """A bridge's own status: version, app baseline and per-capability levels."""
        entry = bridges.for_engine(engine)
        if entry is None:
            return _fail(f"no bridge is answering for {engine}", engine=engine)
        return _ok(engine=engine, endpoint=entry["endpoint"], bridge=bridges.status(entry["endpoint"]))

    @server.tool()
    def bridge_tools(engine: str) -> dict[str, Any]:
        """The tools a bridge publishes, with their descriptions."""
        entry = bridges.for_engine(engine)
        if entry is None:
            return _fail(f"no bridge is answering for {engine}", engine=engine)
        found = bridges.tools(entry["endpoint"])
        return _ok(engine=engine, endpoint=entry["endpoint"], count=len(found), tools=found)

    @server.tool()
    def bridge_call(engine: str, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call any tool on a bridge directly, by its own name.

        The escape hatch for the bridge surface: the vocabulary grows per engine
        and this forwards it without the MCP server needing to know each name.
        """
        entry = bridges.for_engine(engine)
        if entry is None:
            return _fail(f"no bridge is answering for {engine}", engine=engine)
        payload = bridges.call(entry["endpoint"], tool, arguments or {})
        payload.setdefault("engine", engine)
        payload.setdefault("endpoint", entry["endpoint"])
        return payload

    @server.tool()
    def bridge_inject(engine: str, path: str = "") -> dict[str, Any]:
        """Load the native bridge DLL into a running editor (VoiSona, VOCALOID6).

        CeVIO and Synthesizer V do not use this: CeVIO's bridge loads at start-up
        through its launcher, and SynthV's runs in the editor's own Lua host.  The
        DLL is loaded the same way the agent is - LoadLibrary from inside - and
        then announces itself as an endpoint.
        """
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        dll = bridges.native_bridge_dll() if not path else bridges.native_bridge_dll().parent / path
        result = inject.inject(live.pid, dll=dll, wait=10.0)
        if not result.ok:
            return _fail(result.error, engine=engine, pid=live.pid, dll=str(dll))
        entry = bridges.for_engine(engine)
        return _ok(engine=engine, pid=live.pid, dll=str(dll), hmodule=hex(result.hmodule), endpoint=entry["endpoint"] if entry else None)

    @server.tool()
    def bridge_eject(engine: str) -> dict[str, Any]:
        """Unload the native bridge from an editor, through its own unload export."""
        live, problem = _engine(engine)
        if problem:
            return problem
        assert live is not None
        dll = bridges.native_bridge_dll()
        result = inject.unload_module(live.pid, dll.name, image=dll, export=bridges.EJECT_EXPORT)
        result["engine"] = engine
        return result

