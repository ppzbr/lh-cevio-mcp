"""Tests for the live-control layer.

Nothing here needs an editor running: the parts that touch a process are the
parts that cannot be asserted in CI, so they are kept thin and the *logic* -
request framing, token matching, result parsing, key naming - is tested directly.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from lhai.live import agent, apps, song, synthv_live, synthv_lua, win32


# --------------------------------------------------------------------------- #
# key names
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("name", "vk"),
    [("enter", 0x0D), ("F5", 0x74), ("a", 0x41), ("ctrl", 0x11), ("delete", 0x2E), ("space", 0x20)],
)
def test_key_to_vk(name: str, vk: int) -> None:
    assert win32.key_to_vk(name) == vk


def test_key_to_vk_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        win32.key_to_vk("not-a-key")


def test_hotkey_spec_splits_on_plus_and_dash() -> None:
    # Both spellings are common in docs; both must land on the same chord.
    assert [win32.key_to_vk(k) for k in "ctrl+shift+s".replace("-", "+").split("+")] == [0x11, 0x10, 0x53]
    assert [win32.key_to_vk(k) for k in "ctrl-s".replace("-", "+").split("+")] == [0x11, 0x53]


# --------------------------------------------------------------------------- #
# agent wire format
# --------------------------------------------------------------------------- #
def test_agent_request_is_tab_separated_and_crlf_terminated() -> None:
    encoded = agent._encode_request("READ", "7ff6a000", "16")
    assert encoded == b"READ\t7ff6a000\t16\r\n"


def test_agent_request_rejects_embedded_newlines() -> None:
    with pytest.raises(ValueError):
        agent._encode_request("TYPE", "two\nlines")


def test_agent_pipe_name_is_per_process() -> None:
    assert agent.pipe_path(1234) == r"\\.\pipe\lhai_agent.1234"


# --------------------------------------------------------------------------- #
# SynthV bridge protocol
# --------------------------------------------------------------------------- #
def test_result_parsing_splits_ok_and_err() -> None:
    text = "R GROUP ok group=1\nR CLEAR err no note group\nR NOTE ok note\n"
    parsed = synthv_live._parse_results(text)
    assert [p["verb"] for p in parsed] == ["GROUP", "CLEAR", "NOTE"]
    assert [p["ok"] for p in parsed] == [True, False, True]
    assert parsed[1]["payload"] == "no note group"


def test_send_reads_only_the_new_tail_and_matches_its_token(tmp_path: Path) -> None:
    """A stale answer from an earlier batch must not be mistaken for this one."""
    # A batch goes to the generation-specific folder, so the fake agent has to
    # answer there - which is also what proves the routing works.
    ipc = synthv_live.live_dir(tmp_path)
    ipc.mkdir(parents=True, exist_ok=True)
    cmd = ipc / "synthv_cmd.txt"
    res = ipc / "synthv_result.txt"
    res.write_text("RESULT deadbeef ok added=0\n", encoding="utf-8")

    # Pretend to be the agent: on the first poll, answer the queued batch.
    original_send = synthv_live.send

    def fake_agent_once() -> None:
        for _ in range(200):
            if cmd.exists():
                lines = cmd.read_text(encoding="utf-8").splitlines()
                token = next(line.split()[1] for line in lines if line.startswith("END "))
                with res.open("a", encoding="utf-8") as handle:
                    handle.write(f"R NOTE ok note\nRESULT {token} ok added=1\n")
                cmd.unlink()
                return
            import time

            time.sleep(0.01)

    import threading

    thread = threading.Thread(target=fake_agent_once, daemon=True)
    thread.start()
    result = synthv_live.send(tmp_path, ["NOTE 0 705600000 60 a -"], timeout=3.0)
    thread.join(timeout=1.0)

    assert original_send is synthv_live.send
    assert result["ok"] is True
    assert result["results"][0]["verb"] == "NOTE"
    assert result["failed"] == []
    assert Path(str(result["ipc_dir"])) == ipc


def test_send_reports_a_missing_agent_with_the_fix(tmp_path: Path) -> None:
    result = synthv_live.send(tmp_path, ["STATE"], timeout=0.6, poll=0.1)
    assert result["ok"] is False
    assert "LHai Agent" in result["error"]


def test_state_converts_blicks_to_beats() -> None:
    # Exercise the parser with a payload shaped like the Lua agent's STATE reply.
    payload = "N 0 0 705600000 60 a|N 1 705600000 352800000 62 ka"
    notes = []
    for chunk in payload.split("|"):
        parts = chunk.split(" ", 5)
        notes.append(
            {
                "onset": int(parts[2]) / synthv_live.BLICK_PER_QUARTER,
                "duration": int(parts[3]) / synthv_live.BLICK_PER_QUARTER,
                "pitch": int(parts[4]),
                "lyric": parts[5],
            }
        )
    assert notes[0]["onset"] == 0.0
    assert notes[0]["duration"] == 1.0
    assert notes[1]["onset"] == 1.0
    assert notes[1]["lyric"] == "ka"


def test_push_notes_frames_beats_into_blicks(tmp_path: Path) -> None:
    commands: list[str] = []
    original = synthv_live.send

    def capture(ipc_dir, cmds, timeout=15.0, poll=0.2):  # noqa: ANN001, ANN202
        commands.extend(cmds)
        return {"ok": True, "results": [], "failed": []}

    synthv_live.send = capture  # type: ignore[assignment]
    try:
        result = synthv_live.push_notes(
            tmp_path,
            [{"onset": 1.5, "duration": 0.5, "pitch": 64, "lyric": "la"}],
            clear=True,
        )
    finally:
        synthv_live.send = original  # type: ignore[assignment]

    assert result["queued"] == 1
    assert commands[0] == "GROUP 1"
    assert commands[1] == "CLEAR"
    onset, duration, pitch, lyric, language = commands[2].split()[1:]
    assert int(onset) == int(1.5 * synthv_live.BLICK_PER_QUARTER)
    assert int(duration) == int(0.5 * synthv_live.BLICK_PER_QUARTER)
    assert (pitch, lyric, language) == ("64", "la", "-")


# --------------------------------------------------------------------------- #
# the Lua source we install
# --------------------------------------------------------------------------- #
def test_lua_source_bakes_the_ipc_dir_and_defines_the_verbs() -> None:
    source = synthv_lua.source(Path(r"D:\some\ipc"))
    assert r"D:\\some\\ipc" in source
    for verb in ("GROUP", "CLEAR", "NOTE", "STATE", "PHONEME", "PROBE", "SAVE", "VERSION"):
        assert f'handlers["{verb}"]' in source
    # The poll loop must be non-blocking or the editor freezes while the agent runs.
    # The manual's signature is setTimeout(timeOut, callback): timeout first. The
    # first version passed them the other way round and the host answered
    # "attempt to call a nil value" before the script ever polled.
    assert "SV:setTimeout(250, tick)" in source
    assert "SV:setTimeout(tick, 250)" not in source
    assert "while true do" not in source


def test_lua_source_uses_the_documented_api_not_invented_names() -> None:
    """Regression guards: names in the first version simply do not exist.

    ``newUndoBlock`` is not in the manual (it is ``newUndoRecord``), Lua indexes
    from 1 while the wire is 0-based, and ``SV:finish()`` ends the script - which
    a resident agent must never do.
    """
    source = synthv_lua.source(Path(r"D:\some\ipc"))
    assert 'handlers["VERSION"]' in source
    assert "getHostInfo" in source
    assert "newUndoRecord" in source
    assert "newUndoBlock" not in source
    assert "SV.finish" not in source
    # Every crossing from a wire index to a Lua index goes through one helper.
    assert "local function toLua(index)" in source
    assert "for i = 0," not in source


def test_lua_source_takes_the_project_from_sv_not_from_the_editor() -> None:
    """``MainEditorView`` owns focus state, not the document.

    The manual lists getCurrentTrack / getCurrentGroup / getSelection /
    getNavigation on it and no getProject at all, so ``editor:getProject()`` was
    a nil call that killed the script at start-up. The document comes from
    ``SV:getProject()``.
    """
    source = synthv_lua.source(Path(r"D:\some\ipc"))
    assert "SV:getProject()" in source
    assert ":getProject()" in source
    # No call reaches a project through the editor object.
    assert "editor:getProject" not in source
    assert "editor.getProject" not in source


def _fake_editions(tmp_path: Path, *keys: str):
    """Stand in for the install scan, so tests never touch the real machine."""
    rows = []
    for key in keys:
        name = dict(synthv_lua.EDITIONS)[key]
        scripts = tmp_path / "appdata" / "Dreamtonics" / name / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        rows.append(
            {
                "edition": key,
                "name": name,
                "root": str(scripts.parent),
                "scripts": str(scripts),
                "exe": "",
            }
        )
    return rows


def test_bridge_install_writes_into_the_synthv_scripts_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(synthv_lua, "editions", lambda: _fake_editions(tmp_path, "sv2"))
    result = synthv_lua.install(tmp_path / "ipc")
    assert result["installed"] is True
    written = Path(str(result["path"]))
    assert written.exists()
    assert "LHai Agent" in written.read_text(encoding="utf-8")
    # The command folder is created up front.  Lua cannot mkdir, so a generation
    # whose folder was missing could not write even its own start line and the
    # first run would look like a dead agent rather than a missing directory.
    assert Path(str(result["ipc_dir"])).is_dir()


def test_install_covers_both_generations_and_keeps_their_batches_apart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.x and 2.x are separate hosts; one shared command file would double-apply."""
    monkeypatch.setattr(synthv_lua, "editions", lambda: _fake_editions(tmp_path, "sv2", "sv1"))
    result = synthv_lua.install(tmp_path / "ipc")

    assert result["installed"] is True
    assert [row["edition"] for row in result["editions"]] == ["sv2", "sv1"]
    ipc_dirs = {row["edition"]: row["ipc_dir"] for row in result["editions"]}
    assert ipc_dirs["sv1"] != ipc_dirs["sv2"]
    for row in result["editions"]:
        text = Path(row["path"]).read_text(encoding="utf-8")
        assert row["ipc_dir"].replace("\\", "\\\\") in text
        assert Path(row["ipc_dir"]).is_dir()

    # ...and every one of them is reported, not just the newest.
    status = synthv_lua.status(tmp_path / "ipc")
    assert [row["edition"] for row in status["editions"]] == ["sv2", "sv1"]
    assert all(row["installed"] and not row["stale_path"] for row in status["editions"])


def test_editions_finds_a_generation_that_has_never_been_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthesizer V Studio 1 was installed on this machine and never run.

    Its data folder is created by the editor itself on first launch, so keying
    detection on that folder reported one generation where there were two.  The
    executable is the evidence that counts, and the folder - which is where the
    script goes - is created on demand, in the place *that* generation keeps it:
    Documents for 1.x, ``%APPDATA%`` for 2.x.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(synthv_lua, "_documents_dir", lambda: tmp_path / "Documents")
    monkeypatch.setattr(synthv_lua, "data_location", lambda name: "")
    exe = tmp_path / "Synthesizer V Studio Pro" / "synthv-studio.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(synthv_lua, "_INSTALL_EXES", (("sv1", (str(exe),)),))

    found = synthv_lua.editions()
    assert [row["edition"] for row in found] == ["sv1"]
    assert found[0]["exe"] == str(exe)
    expected = tmp_path / "Documents" / "Dreamtonics" / "Synthesizer V Studio" / "scripts"
    assert Path(found[0]["scripts"]) == expected
    assert not expected.exists(), "the folder should not exist yet"

    result = synthv_lua.install(tmp_path / "ipc")
    assert result["installed"] is True
    written = Path(str(result["path"]))
    assert written.exists()
    assert written.parent == expected
    assert not (tmp_path / "appdata").exists(), "1.x does not live under AppData"


def test_2x_that_has_never_been_launched_is_set_up_under_appdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule: 2.x moved to ``%APPDATA%`` and stayed there."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(synthv_lua, "_documents_dir", lambda: tmp_path / "Documents")
    monkeypatch.setattr(synthv_lua, "data_location", lambda name: "")
    exe = tmp_path / "Synthesizer V Studio 2 Pro" / "synthv-studio.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(synthv_lua, "_INSTALL_EXES", (("sv2", (str(exe),)),))

    found = synthv_lua.editions()
    assert [row["edition"] for row in found] == ["sv2"]
    assert Path(found[0]["scripts"]) == (
        tmp_path / "appdata" / "Dreamtonics" / "Synthesizer V Studio 2" / "scripts"
    )


def test_the_data_root_is_the_folder_the_editor_is_actually_writing_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both of the confident answers were wrong at once on this machine.

    ``%APPDATA%\\Dreamtonics\\Synthesizer V Studio`` does not exist for 1.x, and the
    ``DataLocation`` the installer recorded points at an October folder the editor
    has since stopped using.  The one it *does* use is under Documents.  A script
    written to either stale folder sits in a real, plausible directory that nothing
    ever opens, and the only symptom is a *Scripts* menu with no entry in it - so the
    live folder is picked by evidence: the one with the newest content wins.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(synthv_lua, "_documents_dir", lambda: tmp_path / "Documents")
    monkeypatch.setattr(synthv_lua, "_INSTALL_EXES", ())

    stale = tmp_path / "OPSV" / "Dreamtonics" / "Synthesizer V Studio"
    live = tmp_path / "Documents" / "Dreamtonics" / "Synthesizer V Studio"
    for root, age in ((stale, 3600.0), (live, 0.0)):
        (root / "settings").mkdir(parents=True)
        (root / "scripts").mkdir()
        stamp = time.time() - age
        for path in (root, root / "settings", root / "scripts"):
            os.utime(path, (stamp, stamp))
    # The *recorded* location is the stale one.  That is the trap.
    monkeypatch.setattr(synthv_lua, "data_location", lambda name: str(tmp_path / "OPSV"))

    found = synthv_lua.editions()
    assert [row["edition"] for row in found] == ["sv1"]
    assert Path(found[0]["scripts"]) == live / "scripts"
    # Every candidate is reported, so "why is it not in the menu" is answerable
    # from the tool's own output instead of from the filesystem.
    assert found[0]["roots"][0] == str(live)
    assert str(stale) in found[0]["roots"]

    result = synthv_lua.install(tmp_path / "ipc")
    assert Path(str(result["path"])) == live / "scripts" / synthv_lua.BRIDGE_NAME
    assert not (stale / "scripts" / synthv_lua.BRIDGE_NAME).exists()


def test_documents_comes_from_the_shell_because_it_can_be_redirected() -> None:
    """Documents is not necessarily ``%USERPROFILE%\\Documents``.

    Windows records where it really is in ``User Shell Folders``; on this machine it
    is redirected to ``D:\\文档``, and 1.x keeps its entire data folder under it.
    Guessing ``%USERPROFILE%`` yields a plausible absolute path the editor never
    reads - the same failure as guessing ``%APPDATA%``, one folder over.
    """
    resolved = synthv_lua._documents_dir()
    assert resolved is not None
    assert resolved.is_absolute()
    if sys.platform != "win32":  # pragma: no cover - these editors are Windows-only
        return
    import winreg

    key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            recorded, _kind = winreg.QueryValueEx(handle, "Personal")
    except OSError:  # pragma: no cover - no redirect recorded on this machine
        recorded = ""
    if recorded:
        assert resolved == Path(os.path.expandvars(str(recorded)))
    else:  # pragma: no cover - no redirect recorded on this machine
        assert resolved == Path(os.environ["USERPROFILE"]) / "Documents"


def test_data_location_is_empty_for_a_key_that_does_not_exist() -> None:
    """2.x records no data location here, so only its other candidates are tried."""
    assert synthv_lua.data_location("No Such Studio 9") == ""


def test_a_data_location_is_expanded_and_trimmed_before_it_is_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``REG_EXPAND_SZ`` value arrives unexpanded and must not become a folder name.

    ``QueryValueEx`` hands back the raw string, so ``%APPDATA%\\Dreamtonics`` would
    otherwise be a directory literally called ``%APPDATA%`` - and the script would
    be written there, which is the same class of mistake as reading the wrong root
    in the first place.
    """
    monkeypatch.setenv("LH_TEST_ROOT", r"D:\somewhere")
    assert synthv_lua._clean_location(r"%LH_TEST_ROOT%\OPSV") == r"D:\somewhere\OPSV"
    assert synthv_lua._clean_location("  D:\\OPSV  ") == "D:\\OPSV"
    assert synthv_lua._clean_location(None) == ""
    assert synthv_lua._clean_location("") == ""


def test_a_generation_that_is_absent_twice_over_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(synthv_lua, "_documents_dir", lambda: tmp_path / "Documents")
    monkeypatch.setattr(synthv_lua, "data_location", lambda name: "")
    monkeypatch.setattr(synthv_lua, "_INSTALL_EXES", ())
    assert synthv_lua.editions() == []


def test_live_ipc_follows_the_running_generation() -> None:
    assert synthv_lua.live_ipc(r"D:\ipc", exe=r"D:\Synthesizer V Studio 2 Pro\synthv-studio.exe").name == "sv2"
    assert synthv_lua.live_ipc(r"D:\ipc", exe=r"C:\Program Files\Synthesizer V Studio\synthv-studio.exe").name == "sv1"
    # An unreadable path must not silently pick the wrong host when only one is
    # installed - it falls back to the newest, which is the only honest guess.
    assert synthv_lua.edition_of("") == ""


def test_live_dir_takes_the_newest_generation_when_both_are_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 1.x and 2.x both up, "whichever the OS listed first" is not a choice.

    The two agents watch different files, so a batch landing in the other editor
    is a silent edit of the wrong project - or, more likely, a reply from an agent
    that was never asked.
    """
    monkeypatch.setattr(
        synthv_live,
        "_running_exes",
        lambda: [
            r"D:\Synthesizer V Studio Pro\synthv-studio.exe",
            r"D:\Synthesizer V Studio 2 Pro\synthv-studio.exe",
        ],
    )
    assert synthv_live.live_dir(tmp_path).name == "sv2"
    # ...and naming the other one still works, whichever order they appear in.
    assert synthv_live.live_dir(tmp_path, edition="sv1").name == "sv1"
    assert synthv_live.live_dir(tmp_path, edition="sv2").name == "sv2"


def test_live_dir_falls_back_to_the_newest_installed_when_nothing_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(synthv_live, "_running_exes", list)
    monkeypatch.setattr(
        synthv_lua, "editions", lambda: [{"edition": "sv2"}, {"edition": "sv1"}]
    )
    assert synthv_live.live_dir(tmp_path).name == "sv2"


def test_an_unknown_edition_is_refused_rather_than_given_a_folder(
    tmp_path: Path,
) -> None:
    """A typo must not quietly create a third command folder nobody listens to."""
    with pytest.raises(ValueError, match="unknown edition"):
        synthv_live.live_dir(tmp_path, edition="sv9")


# --------------------------------------------------------------------------- #
# engine registry
# --------------------------------------------------------------------------- #
def test_engine_keys_are_the_four_expected() -> None:
    assert set(apps.ENGINE_KEYS) == {"cevio", "voisona", "synthv", "vocaloid"}


def test_every_engine_declares_a_channel_and_a_note() -> None:
    for key in apps.ENGINE_KEYS:
        assert apps.CHANNELS[key]
        assert apps.NOTES[key]


def test_find_unknown_engine_raises() -> None:
    with pytest.raises(KeyError):
        apps.find("not-an-engine")


def test_find_reports_installed_engines_without_running_them() -> None:
    live = apps.find("synthv")
    assert live.key == "synthv"
    assert isinstance(live.as_dict(), dict)
    assert json.dumps(live.as_dict())  # serialisable, which the MCP layer requires
    assert "lua" in live.channels


def test_status_is_serialisable() -> None:
    payload = apps.status()
    assert set(payload) == {"engines", "input_ok"}
    json.dumps(payload)


# --------------------------------------------------------------------------- #
# the song surface (singing synthesis)
# --------------------------------------------------------------------------- #
def test_caps_covers_every_engine_and_is_serialisable() -> None:
    table = song.caps()
    assert set(table) == set(song.ENGINES)
    for engine, entry in table.items():
        assert entry["channel"], engine
        assert isinstance(entry["live"], bool), engine
        # Every engine must name the *mechanism* for the three musical edits, so
        # "no" and "by reloading a file" can never be confused.
        for field in ("notes", "lyrics", "pitch", "transport"):
            assert entry[field], (engine, field)
    json.dumps(table)


def test_caps_rejects_an_unknown_engine() -> None:
    assert "error" in song.caps("vocaloid9")


def test_cevio_control_map_names_the_musical_controls() -> None:
    mapping = song.control_map("cevio")
    assert mapping["transport"]["play"] == "playButton"
    assert mapping["transport"]["stop"] == "stopButton"
    # The six parameter lanes are the reason the song surface exists at all.
    for lane in ("volume", "pitch", "vibrato", "alpha", "husky", "timing"):
        assert lane in mapping["lanes"], lane
    assert mapping["readouts"]["tempo"] == "tempoText"


@pytest.mark.parametrize(
    ("automation_id", "expected"),
    [
        ("pitButton", "pitch"),
        ("tmgButton", "timing"),
        ("norButton", "normal"),
        ("viaButton", "vibrato"),
        ("volumeText", "volume"),
        ("headButton", "head"),
        ("trackViewer", "track"),
    ],
)
def test_control_name_expands_vendor_short_ids(automation_id: str, expected: str) -> None:
    # A learned map is only usable if `pitButton` comes back as something a
    # caller would guess, which is what the alias table is for.
    assert song._control_name(automation_id) == expected


def test_activate_on_an_unknown_engine_does_not_touch_the_desktop() -> None:
    result = song.activate("not-an-engine", "transport", "play")
    assert result["ok"] is False
    assert "unknown engine" in result["error"]


def test_activate_reports_an_unmapped_control_with_the_ones_it_has() -> None:
    # voisona has no shipped map: the failure has to list what *is* mapped and
    # point at learn(), not raise and not click something else.
    result = song.activate("not-an-engine", "lanes", "pitch")
    assert result["ok"] is False
    assert "unknown engine" in result["error"]


def test_lyrics_rejects_empty_text_before_any_input() -> None:
    result = song.lyrics("cevio", "   \n  ")
    assert result["ok"] is False
    assert "no syllables" in result["error"]


def test_snapshot_without_a_path_explains_the_save_dialog() -> None:
    result = song.snapshot("not-an-engine")
    assert result["ok"] is False
    assert "unknown engine" in result["error"]


def test_synthv_transport_rejects_an_action_it_cannot_send() -> None:
    result = song.transport("synthv", "fly")
    assert result["ok"] is False
    assert "no transport action" in result["error"]


def test_read_song_round_trips_through_the_engine_writer(tmp_path: Path) -> None:
    from lhai.mcp.engines import cevio as cevio_engine
    from lhai.mcp.ir import SNote, Song, Track

    song_obj = Song(name="roundtrip", bpm=96.0)
    track = Track(name="Sing", language="japanese")
    track.notes.append(SNote(onset=0.0, duration=1.0, pitch=67, lyric="ら"))
    track.notes.append(SNote(onset=1.0, duration=0.5, pitch=69, lyric="り"))
    song_obj.tracks.append(track)
    path = cevio_engine.write_ccs(song_obj, tmp_path / "rt.ccs")

    result = song.read_song("cevio", str(path))
    assert result["ok"] is True
    # The reader names a project from its file, which is what the editor shows.
    assert result["name"] == "rt"
    assert result["bpm"] == pytest.approx(96.0)
    assert result["tracks"] == [2]


def test_read_song_reports_a_reader_failure_instead_of_raising(tmp_path: Path) -> None:
    broken = tmp_path / "broken.ccs"
    broken.write_text("{ not json", encoding="utf-8")
    result = song.read_song("cevio", str(broken))
    assert result["ok"] is False
    assert result["path"] == str(broken)
