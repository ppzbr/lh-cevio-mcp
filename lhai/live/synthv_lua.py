"""The resident Lua agent for Synthesizer V Studio - source and installer.

SynthV's Lua API is the only *sanctioned* live channel among the four editors, so
it gets a real command surface rather than the three verbs the first version had.
The script runs inside the editor, polls a command file with ``SV:setTimeout``
(non-blocking - the editor keeps painting), executes a batch, and appends one
result line per command plus a final ``RESULT <token>`` line.

Four rules shape the script:

* **Never invent API.**  Every optional call (playback, phonemes, parameters,
  saving) is wrapped in ``pcall`` and reported as unsupported instead of
  throwing inside the poll loop.  ``PROBE`` reports what this particular build
  actually exposes and ``VERSION`` reports which host it is running in, so a
  caller can tell "did nothing" apart from "cannot do that here".
* **One batch, one undo record.**  A batch is wrapped in
  ``project:newUndoRecord()``, so a pushed song is one Ctrl+Z away from being
  gone.
* **The manual is the only source of names** - until the host disagrees.  Two
  calls in the first version were plausible and did not exist, and both failed at
  script start: ``SV:setTimeout(callback, ms)`` takes the **timeout first**, and
  ``Editor:getProject()`` is not a method of ``MainEditorView`` at all - the
  project comes from ``SV:getProject()``.  Both are fixed here, and ``PROBE``
  exists so the next such mistake is a reported ``no`` rather than a dialog.
  Where the manual is incomplete, ``PROBE`` prefers the host's own member list.
* **A second run takes over.**  Lua cannot be reloaded inside a live host, so
  before this every edit to the agent cost a restart of the editor.  Each instance
  stamps its own id into ``ipc/synthv_agent.txt`` at start and checks it on every
  turn; an instance that finds someone else's stamp calls ``SV:finish()`` and
  stops.  A *missing* stamp file means no retirement: losing the marker must not
  take down the only live agent.

  What has been *measured* is narrower than what was designed.  Running the script
  again does leave the newcomer serving - ``PROBE`` says which build answered - and
  no batch is ever applied twice, because the reader that takes the command file
  removes it.  But Synthesizer V has never once written the ``# agent retired``
  line, so the host is evidently stopping the previous instance on its own; the
  menu's *Scripts > Stop all running scripts* is the reliable way back to a known
  state (stop, then run once), and an instance that predates the stamp file cannot
  retire itself at all.

Two traps cost real time and are worth naming, because both looked like "this
build cannot do that":

* **A method that exists is not a method called correctly.**  ``TRACKSEL`` was
  reported as "no way to select a track in this build" because
  ``editor:setCurrentTrack`` was handed a wire index.  It takes a ``Track``
  object, and the number raised.  ``PROBE`` had gone looking for the name, found
  the *call* fail, and reported the absence - the wrong conclusion from correct
  evidence.  The verb now tries ``setCurrentTrack(track)``,
  ``setCurrentGroup(ref)`` and the arrangement's ``selectGroup(ref)`` in order and
  carries the host's own error for each.
* **The host is the authority on its own API, and it will say so.**
  ``pairs()`` works on Synthesizer V's objects, so ``PROBE`` now merges the
  host's own member list into the documented one and reports ``enumerated=<owners>``
  for the owners it got that way.  That is how ``setCurrentTrack``,
  ``setCurrentGroup``, ``playback:pause``, ``SV.QUARTER`` and
  ``project:getTimeAxis`` turned up at all - and, in the other direction, it is
  what *confirmed* the absences: no ``save`` anywhere on ``Project``, no
  ``undo``/``redo`` on the editor or its navigation.  Detection is tried, never
  required; a host that cannot enumerate still gets the documented names.

``MainEditorView`` is a UI-state object: it can report what is in focus
(``getCurrentTrack``, ``getCurrentGroup``, ``getSelection``, ``getNavigation``)
but it does not own the document.  Everything that reads or writes notes goes
through ``SV:getProject()``.

Lua indexes from 1 while this project's wire protocol is 0-based, so every
crossing is converted once, in ``toLua``/``fromLua``, rather than left to the
reader.

Both generations of the editor are supported.  ``Synthesizer V Studio`` (1.x) and
``Synthesizer V Studio 2`` are separate products with separate script folders and
separate Lua hosts, so the agent is installed into every generation that is
present and each one gets its own command/result pair - two running editors must
never claim the same batch.

**The script folder is not always ``%APPDATA%``, and no single rule finds it.**
Two "obvious" answers were both wrong here at once: 1.x has no
``%APPDATA%\\Dreamtonics\\Synthesizer V Studio`` folder at all, and the
``DataLocation`` the installer recorded in
``HKCU\\Software\\Dreamtonics\\<product>`` points at an October install
(``D:\\OPSV``) that the editor has since moved on from.  What 1.x is using is
``D:\\文档\\Dreamtonics\\Synthesizer V Studio``, because it keeps its data under
*Documents* and Documents is redirected to ``D:`` on this machine.  Writing the
script into either stale location put it in a real, plausible, unwatched folder,
and the only symptom was an editor whose *Scripts* menu had no entry in it.

So the folder is not decided by configuration but by evidence: every candidate is
gathered (``%APPDATA%``, Documents as Windows defines it, the recorded
``DataLocation``) and the one the editor is actually writing to wins - an editor in
use rewrites ``recovery``, ``settings`` and ``databases`` constantly, a stale folder
stops changing the day it stops being used.  ``editions()`` reports every candidate
it considered, because this is a failure that is invisible from the outside.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "BRIDGE_NAME",
    "EDITIONS",
    "edition_of",
    "editions",
    "data_location",
    "scripts_dir",
    "ipc_for_edition",
    "live_ipc",
    "source",
    "install",
    "status",
]

BRIDGE_NAME = "LHai_Agent.lua"

#: Registry key each product records its data folder under.
_REGISTRY_ROOT = r"Software\Dreamtonics"

#: Every generation, newest first.  The name is the product folder under the data
#: root *and* the registry key its data root is recorded in - the same word in both
#: places, which is why one tuple serves for the two lookups.
EDITIONS: tuple[tuple[str, str], ...] = (
    ("sv2", "Synthesizer V Studio 2"),
    ("sv1", "Synthesizer V Studio"),
)

#: How to tell the generations apart from a process path.  Both ship the same
#: image name (``synthv-studio.exe``), so only the install location separates
#: them - and "Studio 2" is checked first because a 2.x install path also
#: contains the 1.x folder name.
EDITION_MARKERS: tuple[tuple[str, str], ...] = (
    ("sv2", "Studio 2"),
    ("sv1", "Synthesizer V Studio"),
)

#: Where each generation puts its executable.  Deliberately *not* shared with
#: ``lhai.mcp.engines.base``: that list answers "which engine is the default" and
#: stops at the first hit, while this one answers "which generations are
#: installed" and must not stop - both being present is the case it exists for.
#: The paths do agree, and SV1's are the same ones base already carries.
_INSTALL_EXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("sv2", (
        r"D:\Synthesizer V Studio 2 Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio 2 Pro\synthv-studio.exe",
    )),
    ("sv1", (
        r"D:\Synthesizer V Studio Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio Pro\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio Basic\synthv-studio.exe",
        r"D:\Synthesizer V Studio Flat\synthv-studio.exe",
        r"C:\Program Files\Synthesizer V Studio\synthv-studio.exe",
        r"D:\Synthesizer V Studio\synthv-studio.exe",
        r"C:\Program Files (x86)\Synthesizer V Studio\synthv-studio.exe",
    )),
)


def _clean_location(value: object) -> str:
    """A registry ``DataLocation`` value as a usable path.

    ``%USERPROFILE%``-style indirection is expanded, because a value stored as
    ``REG_EXPAND_SZ`` arrives unexpanded from ``QueryValueEx`` and left alone would
    become a literal directory named ``%APPDATA%``.  Surrounding whitespace goes
    too, since a padded value is a padded folder name.
    """
    return os.path.expandvars(str(value or "")).strip()


def _registry_value(path: str, value: str) -> str:
    """One value read out of ``HKCU`` and cleaned, or ``""`` if it is not there."""
    if sys.platform != "win32":  # pragma: no cover - these editors are Windows-only
        return ""
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows guard
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            read, _kind = winreg.QueryValueEx(key, value)
    except OSError:
        return ""
    return _clean_location(read)


def data_location(name: str) -> str:
    """The data root one generation was *told* to use, or ``""``.

    Dreamtonics records it in ``HKCU\\Software\\Dreamtonics\\<product>`` as
    ``DataLocation``.  It is not what the editor necessarily uses *now* - this
    machine has 1.x pointed at ``D:\\OPSV`` while the running editor writes to
    ``D:\\文档`` - so it is only one candidate among the places a data folder can
    live, and the least trusted of them.  See :func:`_data_root`.
    """
    return _registry_value(_REGISTRY_ROOT + "\\" + name, "DataLocation")


def _documents_dir() -> Path | None:
    """The user's Documents folder, which is not always under ``%USERPROFILE%``.

    Windows records the real location in ``User Shell Folders``; on this machine
    it is redirected to ``D:\\文档``, and 1.x keeps its **entire** data folder under
    it.  Guessing ``%USERPROFILE%\\Documents`` produces a plausible absolute path
    that the editor never reads - the same failure as guessing ``%APPDATA%``, one
    folder over.
    """
    recorded = _registry_value(
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        "Personal",
    )
    if recorded:
        return Path(recorded)
    home = os.environ.get("USERPROFILE")
    if home:
        return Path(home) / "Documents"
    return None


def _candidate_roots(key: str, name: str) -> list[Path]:
    """Every folder this generation may keep its data in, in preference order.

    The order only decides where a generation that has never run is set up, because
    :func:`_data_root` picks among the ones that exist by how recently they were
    written to.  2.x keeps its data under ``%APPDATA%``; 1.x keeps it under
    Documents; and a recorded ``DataLocation`` is honoured last, since it survives
    reinstalls and data-folder changes that the editor has since moved on from.
    """
    appdata = os.environ.get("APPDATA")
    documents = _documents_dir()
    location = data_location(name)
    candidates = {
        "appdata": Path(appdata) / "Dreamtonics" / name if appdata else None,
        "documents": documents / "Dreamtonics" / name if documents else None,
        "recorded": Path(location) / "Dreamtonics" / name if location else None,
    }
    order = (
        ("appdata", "documents", "recorded")
        if key == "sv2"
        else ("documents", "appdata", "recorded")
    )
    return [candidate for label in order if (candidate := candidates[label]) is not None]


def _freshness(root: Path) -> float:
    """Newest mtime of a data folder or its immediate children, or ``-1.0``.

    An editor in use keeps writing into its data folder - ``recovery`` on every
    project change, ``settings`` on exit, ``databases`` as it caches voices - so
    the folder it is really using is the one with the newest content.  A stale
    folder from an earlier install stops changing the day it stops being used.
    """
    try:
        newest = root.stat().st_mtime
    except OSError:
        return -1.0
    try:
        entries = list(root.iterdir())
    except OSError:
        return newest
    for entry in entries:
        try:
            newest = max(newest, entry.stat().st_mtime)
        except OSError:
            continue
    return newest


def _data_root(key: str, name: str) -> Path | None:
    """The folder this generation is *actually* using, or ``None`` if unknowable.

    Decided by evidence rather than by configuration, because both the obvious
    answers were wrong here at once: ``%APPDATA%\\Dreamtonics\\Synthesizer V Studio``
    does not exist for 1.x, and the ``DataLocation`` the installer recorded
    (``D:\\OPSV``) is a leftover from an October install while the running editor
    writes to ``D:\\文档\\Dreamtonics\\Synthesizer V Studio``.  Ranking the candidates
    by how recently they were written to lands on the live one, and keeps working
    if the folder is moved again - a moved folder starts being written to
    immediately, so it wins the ranking on its own.
    """
    candidates = _candidate_roots(key, name)
    if not candidates:
        return None
    existing = [candidate for candidate in candidates if candidate.is_dir()]
    if existing:
        return max(existing, key=_freshness)
    return candidates[0]


def edition_of(exe: str) -> str:
    """Which generation an executable path belongs to, or ``""`` if unclear."""
    text = (exe or "").replace("/", "\\")
    for key, marker in EDITION_MARKERS:
        if marker in text:
            return key
    return ""


def _install_exe(edition: str) -> str:
    """The executable of one generation, or ``""`` when it isn't installed."""
    for key, candidates in _INSTALL_EXES:
        if key != edition:
            continue
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate
    return ""


def editions() -> list[dict[str, str]]:
    """Every installed generation, newest first.

    A generation counts as installed when its product folder exists *or* its
    executable is where the installer puts it.  Keying on the folder alone was
    wrong twice over: SynthV Studio 1 had never been launched here, so its data
    folder had not been created yet - and the folder that was checked was not the
    one the editor uses anyway.  ``_data_root`` finds the live folder and it is
    created on demand, which is what the editor itself does on first run.

    ``roots`` lists every candidate that was considered, best first, because the
    failure this replaces was invisible: a script written to a perfectly real folder
    that the editor simply never opens.
    """
    found: list[dict[str, str]] = []
    for key, name in EDITIONS:
        root = _data_root(key, name)
        if root is None:
            # No APPDATA, no Documents and no registry entry: there is no place to
            # put the script, so a generation that cannot be reached is reported as
            # nothing rather than with a relative path that would install into the
            # working folder.
            continue
        exe = _install_exe(key)
        if not exe and not root.exists():
            continue
        found.append(
            {
                "edition": key,
                "name": name,
                "root": str(root),
                "scripts": str(root / "scripts"),
                "roots": [str(candidate) for candidate in _candidate_roots(key, name)],
                "exe": exe,
                "data_location": data_location(name),
            }
        )
    return found


def scripts_dir(edition: str | None = None) -> Path | None:
    """One generation's script folder; the newest when none is named."""
    for item in editions():
        if edition is None or item["edition"] == edition:
            return Path(item["scripts"])
    return None


def ipc_for_edition(ipc_dir: str | Path, edition: str) -> Path:
    """Each generation gets its own command/result pair.

    They are separate processes with separate Lua hosts and separate script
    instances.  One shared file would let both agents claim the same batch and
    apply it twice, which is worse than having no channel at all.
    """
    return Path(ipc_dir) / edition


def live_ipc(ipc_dir: str | Path, exe: str = "", edition: str = "") -> Path:
    """Where to talk, for whichever generation is actually running.

    Falls back to the newest installed generation when the process cannot be
    identified, so a single-install machine never has to care about this.
    """
    key = edition or edition_of(exe)
    if key and key not in dict(EDITIONS):
        # A typo must not quietly create a third command folder nobody listens to.
        raise ValueError(
            f"unknown edition {key!r}; expected one of {', '.join(dict(EDITIONS))}"
        )
    if not key:
        installed = editions()
        key = installed[0]["edition"] if installed else "sv2"
    return ipc_for_edition(ipc_dir, key)


def source(ipc_dir: str | Path) -> str:
    return _TEMPLATE.replace("__IPC_DIR__", str(ipc_dir).replace("\\", "\\\\"))


def install(ipc_dir: str | Path) -> dict[str, object]:
    """Write the agent into every installed generation.

    The newest generation's details are mirrored at the top level for callers
    that want a single answer; ``editions`` carries the full list.
    """
    found = editions()
    if not found:
        return {
            "installed": False,
            "reason": "no Synthesizer V Studio generation found - neither a "
            "project folder in %APPDATA% nor an executable in its usual place",
            "editions": [],
        }
    written: list[dict[str, str]] = []
    for item in found:
        target_dir = Path(item["scripts"])
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / BRIDGE_NAME
        ipc = ipc_for_edition(ipc_dir, item["edition"])
        # The command folder has to exist before the script is ever run.  Lua has
        # no mkdir, so an agent started with the folder missing cannot write its
        # own start line or its replies, and the client only sees a timeout - the
        # first run of a newly installed generation would look like a dead agent.
        ipc.mkdir(parents=True, exist_ok=True)
        target.write_text(source(ipc), encoding="utf-8")
        written.append(
            {
                "edition": item["edition"],
                "name": item["name"],
                "path": str(target),
                "ipc_dir": str(ipc),
                "exe": item["exe"],
            }
        )
    head = written[0]
    return {
        "installed": True,
        "path": head["path"],
        "ipc_dir": head["ipc_dir"],
        "editions": written,
        "next": (
            "Synthesizer V: Scripts > LHai Agent. It stays resident, and running "
            "it again leaves the new copy serving; to be sure, use Scripts > Stop "
            "all running scripts first. If the editor is already running a copy "
            "from a build older than the stamp file, close and reopen it once - "
            "that copy cannot retire itself and the host does not stop it either."
        ),
    }


def status(ipc_dir: str | Path) -> dict[str, object]:
    """Per-generation install state, plus the newest generation's flat keys."""
    rows: list[dict[str, object]] = []
    for item in editions():
        ipc = ipc_for_edition(ipc_dir, item["edition"])
        script = Path(item["scripts"]) / BRIDGE_NAME
        installed = script.exists()
        stale = False
        if installed:
            text = script.read_text(encoding="utf-8", errors="replace")
            stale = str(ipc).replace("\\", "\\\\") not in text
        rows.append(
            {
                "edition": item["edition"],
                "name": item["name"],
                "installed": installed,
                "path": str(script),
                "stale_path": stale,
                "ipc_dir": str(ipc),
                "command_file": str(ipc / "synthv_cmd.txt"),
                "result_file": str(ipc / "synthv_result.txt"),
                # Holds the running instance's stamp; internal, but named here so
                # a third file appearing in the folder is not a surprise.
                "instance_file": str(ipc / "synthv_agent.txt"),
            }
        )
    head = rows[0] if rows else None
    return {
        "installed": bool(head and head["installed"]),
        "path": str(head["path"]) if head else "",
        "stale_path": bool(head and head["stale_path"]),
        "ipc_dir": str(head["ipc_dir"]) if head else "",
        "command_file": str(head["command_file"]) if head else "",
        "result_file": str(head["result_file"]) if head else "",
        "instance_file": str(head["instance_file"]) if head else "",
        "editions": rows,
    }


_TEMPLATE = r'''-- LHai_Agent.lua - resident control agent for the LH AI Studio MCP server.
-- Generated by lhai.live.synthv_lua; edit that module, not this copy.
--
--   Synthesizer V: Scripts > LHai Agent   (running it again leaves the new copy
--                                            serving; Scripts > Stop all running
--                                            scripts is the sure way.  A copy from
--                                            a build older than the instance file
--                                            below can neither replace itself nor
--                                            be stopped by the host - close and
--                                            reopen the editor once for that one)
--
-- Command file : __IPC_DIR__\synthv_cmd.txt
-- Result file  : __IPC_DIR__\synthv_result.txt
-- Instance file: __IPC_DIR__\synthv_agent.txt   (this instance's stamp; another
--                                                stamp here retires this copy)
--
-- Protocol: one command per line, whitespace separated, batch ends with
-- "END <token>".  Every command appends "R <verb> <ok|err> <payload>"; the
-- batch ends with "RESULT <token> ..." so the client knows it can stop waiting.
--
-- Indexing: Lua indexes from 1, the wire protocol from 0.  Everything that
-- crosses the boundary goes through toLua() / fromLua(), so the handlers below
-- can be read in wire terms throughout.
local BLICK = 705600000
local DIR = "__IPC_DIR__\\"
local CMD = DIR .. "synthv_cmd.txt"
local RES = DIR .. "synthv_result.txt"
--: Holds the stamp of the newest agent instance; see main() for why.
local CLAIM = DIR .. "synthv_agent.txt"

function getClientInfo()
  return {
    name = "LHai Agent",
    category = "LH AI Studio",
    author = "lh-ai-studio",
    -- 4: methods are reached through member(), not type(fn) == "function".
    -- 5: LYRIC passes one value; an instance superseded by a new one retires.
    versionNumber = 5,
    minEditorVersion = 65536
  }
end

local function log(text)
  local f = io.open(RES, "a")
  if f then
    f:write(text .. "\n")
    f:close()
  end
end

local function reply(verb, ok, payload)
  -- Newlines would split one reply into two unparseable lines, so they are
  -- flattened here rather than at every call site.
  local text = tostring(payload or ""):gsub("[\r\n]+", " ")
  log("R " .. verb .. " " .. (ok and "ok" or "err") .. " " .. text)
end

local function words(line)
  local out = {}
  for w in line:gmatch("%S+") do out[#out + 1] = w end
  return out
end

-- Attempt a call, returning ok, value.  Used for anything the scripting manual
-- does not promise for this build, so an unsupported verb is reported, not
-- thrown into the poll loop.
--
-- The results are collected into a table first on purpose: assigning
-- "pcall(...)" to two locals keeps only the callee's *first* return value, and
-- callers here need the real one (a Track, a Note, a string).
local function attempt(fn, ...)
  local out = { pcall(fn, ...) }
  if not out[1] then return false, tostring(out[2]) end
  return true, out[2]
end

-- Fetch a method off an object, or nil when there is none.
--
-- Deliberately *not* `type(obj[name]) == "function"`.  A host is free to bind
-- its methods as callable userdata, and Synthesizer V does - so an existence
-- test that insisted on "function" answered "no" for every name in the API,
-- on a host where every one of those calls works.  `SV:setTimeout` was being
-- called and working while this probe reported that it did not exist.
--
-- The value comes back as-is and callers pcall it, so a name that is present
-- but not callable reports an honest error instead of a silent no-op.
local function member(subject, name)
  if not subject then return nil end
  local got, value = attempt(function() return subject[name] end)
  if got and value ~= nil then return value end
  return nil
end

-- Wire indices are 0-based; the Lua API is 1-based.  One place, not five.
local function toLua(index) return (tonumber(index) or 0) + 1 end
local function fromLua(index) return index - 1 end

-- 705600000 blicks per quarter is what every documented build has used.  Asked
-- for anyway, because it is published as SV.QUARTER and a build that changed the
-- tick base would otherwise mis-place every note by a constant factor with no
-- error to show for it.
local function blick()
  local quarter = member(SV, "QUARTER")
  if type(quarter) == "number" and quarter > 0 then return quarter end
  return BLICK
end

-- Lua 5.1 spells it unpack, 5.4 spells it table.unpack.  Needed because a
-- nested function may not reach out to the enclosing one's "..." - doing that
-- is a syntax error, and it is how this script failed to load the first time it
-- was run through a Lua host rather than read by eye.
local unpack_args = table.unpack or unpack

-- SV:getProject() is the document.  SV:getMainEditor() is only the view state:
-- it has getCurrentTrack / getCurrentGroup / getSelection / getNavigation and no
-- getProject, which is why the first version of this script died at start-up.
local function sv_project() return SV:getProject() end
local function main_editor() return SV:getMainEditor() end

local function host_info()
  local ok, value = attempt(function() return SV:getHostInfo() end)
  if ok and type(value) == "table" then return value end
  return nil
end

local function current_track(editor)
  local ok, track = attempt(function() return editor:getCurrentTrack() end)
  if ok and track then return track, nil end
  return nil, "no current track in this build"
end

-- The group the wire index names, on the current track.  A NoteGroupReference is
-- what a track holds; its getTarget() is the NoteGroup that carries the notes.
local function group_at(editor, index)
  local want = math.max(1, tonumber(index) or 1)
  local track = current_track(editor)
  if track then
    local ok, count = attempt(function() return track:getNumGroups() end)
    if ok and type(count) == "number" and count >= 1 then
      want = math.max(1, math.min(want, count))
      local got, ref = attempt(function() return track:getGroupReference(want) end)
      if got and ref then
        local targetOk, target = attempt(function() return ref:getTarget() end)
        if targetOk and target then return target, nil end
        return ref, nil
      end
    end
  end
  -- Fall back to whatever is in focus, which is what the editor itself would edit.
  local ok, current = attempt(function() return editor:getCurrentGroup() end)
  if ok and current then
    local targetOk, target = attempt(function() return current:getTarget() end)
    if targetOk and target then return target, nil end
    return current, nil
  end
  return nil, "no note group on the current track"
end

local function note_list(group)
  local out = {}
  local count = group:getNumNotes()
  -- getNote() is 1-based in Lua; the index written to the wire is 0-based.
  for i = 1, count do
    local note = group:getNote(i)
    if note then
      out[#out + 1] = string.format(
        "N %d %d %d %d %s",
        fromLua(i),
        note:getOnset(),
        note:getDuration(),
        note:getPitch(),
        note:getLyrics() or ""
      )
    end
  end
  return out
end

-- Playback is documented on the PlaybackControl object reached through
-- SV:getPlayback().  Some builds also hang play/stop off the editor, so both are
-- tried and the reply names which one answered - "did nothing" and "worked" have
-- to stay distinguishable.
local function playback_control()
  local ok, control = attempt(function() return SV:getPlayback() end)
  if ok and control then return control end
  return nil
end

local function do_playback(editor_method, control_method, ...)
  -- Copied out of "..." immediately: the closures below cannot use it directly.
  local args = { ... }
  local editor = main_editor()
  if editor_method then
    local fn = member(editor, editor_method)
    if fn then
      if attempt(function() return fn(editor, unpack_args(args)) end) then
        return true, "editor:" .. editor_method
      end
    end
  end
  local control = playback_control()
  if not control then return false, "no playback control in this build" end
  local fn = member(control, control_method)
  if not fn then return false, "playback:" .. control_method .. " unsupported" end
  local ok, problem = attempt(function() return fn(control, unpack_args(args)) end)
  if not ok then return false, tostring(problem) end
  return true, "playback:" .. control_method
end

-- ---------------------------------------------------------------- verbs
local handlers = {}

handlers["GROUP"] = function(state, args)
  state.group = tonumber(args[1]) or 1
  return true, "group=" .. state.group
end

handlers["TRACKSEL"] = function(state, args)
  local project = sv_project()
  local index = tonumber(args[1]) or 0
  local count = project:getNumTracks()
  if index < 0 or index >= count then
    return false, "track out of range (0.." .. (count - 1) .. ")"
  end

  -- Three ways in, tried in order, and the reply says which one answered.  When
  -- none does it carries the host's own error for each: "no way to select a
  -- track" cannot be acted on, and a bare "raised" hides the reason.  Both editor
  -- methods take the *object*, not an index - handing setCurrentTrack a number is
  -- what made it raise the first time round, which read as "this build has no
  -- way in" until the host's own member list said otherwise.
  local tried = {}
  local function record(label, problem)
    tried[#tried + 1] = label .. ": " .. tostring(problem)
  end

  local gotTrack, track = attempt(function() return project:getTrack(toLua(index)) end)
  if not (gotTrack and track) then return false, "no track at " .. index end
  local gotRef, ref = attempt(function() return track:getGroupReference(1) end)
  local editor = main_editor()

  if member(editor, "setCurrentTrack") then
    local ok, problem = attempt(function() return editor:setCurrentTrack(track) end)
    if ok then return true, "editor:setCurrentTrack=" .. index end
    record("editor.setCurrentTrack", problem)
  else
    record("editor.setCurrentTrack", "no such method")
  end

  if gotRef and ref and member(editor, "setCurrentGroup") then
    local ok, problem = attempt(function() return editor:setCurrentGroup(ref) end)
    if ok then return true, "editor:setCurrentGroup=" .. index end
    record("editor.setCurrentGroup", problem)
  else
    record("editor.setCurrentGroup", "no such method")
  end

  local gotArrangement, arrangement = attempt(function() return SV:getArrangement() end)
  local gotSelection, selection = nil, nil
  if gotArrangement and arrangement then
    gotSelection, selection = attempt(function() return arrangement:getSelection() end)
  end
  if not (gotArrangement and arrangement) then
    record("selection.selectGroup", "no arrangement object")
  elseif not (gotSelection and selection) then
    record("selection.selectGroup", "arrangement has no getSelection")
  elseif not (gotRef and ref) then
    record("selection.selectGroup", "track has no group 1")
  elseif not member(selection, "selectGroup") then
    record("selection.selectGroup", "no such method")
  else
    local ok, problem = attempt(function() return selection:selectGroup(ref) end)
    if ok then return true, "arrangement:selectGroup=" .. index end
    record("selection.selectGroup", problem)
  end

  return false, "no way to select a track in this build (" .. table.concat(tried, "; ") .. ")"
end

handlers["TRACKS"] = function()
  local project = sv_project()
  local count = project:getNumTracks()
  local out = {}
  -- getTrack() is 1-based in Lua; the index written to the wire is 0-based.
  for i = 1, count do
    local track = project:getTrack(i)
    local name = "?"
    local groups = 0
    if track then
      local ok, value = attempt(function() return track:getName() end)
      if ok and value then name = value end
      local ok2, value2 = attempt(function() return track:getNumGroups() end)
      if ok2 then groups = value2 end
    end
    out[#out + 1] = string.format("T %d %d %s", fromLua(i), groups, name)
  end
  return true, table.concat(out, "|")
end

handlers["CLEAR"] = function(state)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local count = group:getNumNotes()
  for i = count, 1, -1 do group:removeNote(i) end
  return true, "cleared=" .. count
end

handlers["NOTE"] = function(state, args)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local note = SV:create("Note")
  note:setOnset(tonumber(args[1]) or 0)
  note:setDuration(math.max(1, tonumber(args[2]) or blick()))
  note:setPitch(tonumber(args[3]) or 60)
  local lyric = args[4]
  if lyric and lyric ~= "" and lyric ~= "-" then note:setLyrics((lyric:gsub("_", " "))) end
  local lang = args[5]
  if lang and lang ~= "" and lang ~= "-" then
    attempt(function() note:setLanguageOverride(lang) end)
  end
  group:addNote(note)
  state.added = (state.added or 0) + 1
  return true, "note"
end

handlers["STATE"] = function(state)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local lines = note_list(group)
  return true, table.concat(lines, "|")
end

handlers["LYRIC"] = function(state, args)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local index = tonumber(args[1])
  if not index then return false, "index required" end
  local note = group:getNote(toLua(index))
  if not note then return false, "no note at " .. index end
  -- The outer parentheses are load-bearing: string.gsub returns the new string
  -- *and* the number of substitutions, and without them both are passed to
  -- setLyrics - which answers "expected 1 argument, got 2".
  local lyric = args[2] or ""
  note:setLyrics((lyric:gsub("_", " ")))
  return true, "ok"
end

handlers["DEL"] = function(state, args)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local index = tonumber(args[1])
  if not index then return false, "index required" end
  group:removeNote(toLua(index))
  return true, "ok"
end

handlers["PHONEME"] = function(state, args)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local index = tonumber(args[1])
  if not index then return false, "index required" end
  local note = group:getNote(toLua(index))
  if not note then return false, "no note at " .. index end
  local phonemes = args[2]
  if phonemes == nil or phonemes == "-" then return false, "phonemes required" end
  local ok, problem = attempt(function() note:setPhonemes(phonemes) end)
  if not ok then return false, "setPhonemes unsupported: " .. tostring(problem) end
  return true, "ok"
end

handlers["PITCH"] = function(state, args)
  -- PITCH <semitones> shifts every note in the group.
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local shift = tonumber(args[1]) or 0
  for i = 1, group:getNumNotes() do
    local note = group:getNote(i)
    if note then note:setPitch(note:getPitch() + shift) end
  end
  return true, "shifted=" .. shift
end

handlers["TIMESHIFT"] = function(state, args)
  local group, err = group_at(main_editor(), state.group)
  if not group then return false, err end
  local delta = tonumber(args[1]) or 0
  for i = 1, group:getNumNotes() do
    local note = group:getNote(i)
    if note then note:setOnset(math.max(0, note:getOnset() + delta)) end
  end
  return true, "shifted=" .. delta
end

handlers["SAVE"] = function(state, args)
  local project = sv_project()
  local path = table.concat(args, " ")
  -- The scripting manual documents no save entry point on Project, so this is
  -- tried and then reported honestly rather than pretended.
  local ok = attempt(function() return project:writeFile(path) end)
  if ok then return true, "saved=" .. path end
  local name = "-"
  local gotName, value = attempt(function() return project:getFileName() end)
  if gotName and value then name = value end
  return false, "the scripting API has no save entry point in this build, so the " ..
    "batch stays in memory until the editor saves it. current file: " .. name
end

handlers["SEEK"] = function(state, args)
  local position = tonumber(args[1]) or 0
  local ok, where = do_playback("setPlaybackPosition", "seek", position)
  if not ok then return false, where end
  return true, where .. "=" .. position
end

handlers["PLAY"] = function()
  local ok, where = do_playback("play", "play")
  if not ok then return false, where end
  return true, where
end

handlers["STOP"] = function()
  local ok, where = do_playback("stop", "stop")
  if not ok then
    ok, where = do_playback("pause", "pause")
  end
  if not ok then return false, where end
  return true, where
end

handlers["UNDO"] = function()
  -- The scripting API exposes newUndoRecord() but no undo() entry point.  The
  -- manual's absence is not proof, so both places it could live are tried before
  -- the absence is reported.
  local editor = main_editor()
  local fn = member(editor, "undo")
  if not fn then
    local gotNavigation, navigation = attempt(function() return editor:getNavigation() end)
    if gotNavigation and navigation then fn = member(navigation, "undo") end
  end
  if not fn then
    return false,
      "no undo entry point in this build (tried editor.undo and " ..
      "editor:getNavigation().undo; the scripting API documents newUndoRecord() " ..
      "and no way back)"
  end
  local ok, problem = attempt(function() return fn(editor) end)
  if not ok then return false, tostring(problem) end
  return true, "ok"
end

handlers["REDO"] = function()
  local editor = main_editor()
  local fn = member(editor, "redo")
  if not fn then
    local gotNavigation, navigation = attempt(function() return editor:getNavigation() end)
    if gotNavigation and navigation then fn = member(navigation, "redo") end
  end
  if not fn then
    return false,
      "no redo entry point in this build (tried editor.redo and " ..
      "editor:getNavigation().redo)"
  end
  local ok, problem = attempt(function() return fn(editor) end)
  if not ok then return false, tostring(problem) end
  return true, "ok"
end

handlers["VERSION"] = function()
  local info = host_info()
  if not info then return false, "getHostInfo unsupported in this build" end
  return true, string.format(
    "host=%s|version=%s|number=%s|os=%s|ui=%s",
    tostring(info.hostName or "?"),
    tostring(info.hostVersion or "?"),
    tostring(info.hostVersionNumber or "?"),
    tostring(info.osType or "?"),
    tostring(info.languageCode or "?")
  )
end

handlers["PROBE"] = function()
  local editor = main_editor()
  local project = sv_project()
  local track = current_track(editor)
  local group = group_at(editor, 1)
  local control = playback_control()
  local host = host_info()

  -- The arrangement and its selection are what TRACKSEL has to go through when
  -- the editor has no setCurrentTrack, so they are probed directly rather than
  -- reported as a bare "cannot select a track here".
  local arrangement = nil
  local gotArrangement, arranged = attempt(function() return SV:getArrangement() end)
  if gotArrangement then arrangement = arranged end
  local selection = nil
  if arrangement then
    local gotSelection, selected = attempt(function() return arrangement:getSelection() end)
    if gotSelection then selection = selected end
  end

  -- A throwaway Note is the reliable subject for the note methods: probing an
  -- empty group's getNote(1) would answer "unsupported" for the whole class.
  local probeNote = nil
  local created, made = attempt(function() return SV:create("Note") end)
  if created and made then
    probeNote = made
  elseif group then
    local got, note = attempt(function() return group:getNote(1) end)
    if got then probeNote = note end
  end

  -- Names exactly as the scripting manual writes them, per owner object.  This is
  -- the *floor*, not the answer: a build that answers "no" here answers "no" at
  -- call time too, but a name the manual does not list can still exist, so the
  -- host's own member list is merged in below and usually wins.
  -- `undo`/`redo` are the documented *absence* - the manual lists
  -- newUndoRecord() and no way back - so they are probed as candidates rather
  -- than assumed to be missing.
  local documented = {
    sv = {
      "getProject", "getMainEditor", "getPlayback", "getArrangement",
      "getHostInfo", "create", "finish", "setTimeout", "print"
    },
    editor = {
      "getCurrentTrack", "getCurrentGroup", "getSelection", "getNavigation",
      "undo", "redo"
    },
    arrangement = { "getSelection" },
    selection = { "selectGroup", "unselectGroup", "clearAll" },
    project = {
      "getNumTracks", "getTrack", "getTimeAxis", "getFileName", "getDuration",
      "newUndoRecord", "addTrack", "removeTrack", "addNoteGroup", "getNoteGroup",
      "getNumNoteGroupsInLibrary", "removeNoteGroup"
    },
    track = {
      "getName", "getNumGroups", "getGroupReference", "addGroupReference",
      "removeGroupReference", "getDuration", "getMixer", "getDisplayColor",
      "getDisplayOrder", "isBounced"
    },
    group = {
      "addNote", "removeNote", "getNote", "getNumNotes", "getParameter",
      "getUUID", "getName", "getName", "addPitchControl", "getPitchControl",
      "getNumPitchControls", "removePitchControl"
    },
    note = {
      "setOnset", "getOnset", "setDuration", "getDuration", "getEnd",
      "setPitch", "getPitch", "setLyrics", "getLyrics", "setPhonemes",
      "getPhonemes", "setDetune", "getDetune", "setLanguageOverride",
      "getLanguageOverride", "getMusicalType", "setMusicalType", "setTimeRange",
      "getAttributes", "setAttributes", "getRapAccent", "setRapAccent"
    },
    playback = { "play", "pause", "stop", "seek", "loop", "getPlayhead", "getStatus" }
  }
  local subjects = {
    sv = SV, editor = editor, project = project, track = track, group = group,
    note = probeNote, playback = control,
    arrangement = arrangement, selection = selection
  }

  -- "What does this object actually have?" beats "did the name I guessed exist?"
  -- Synthesizer V answers pairs() on its objects, which turns PROBE from a list of
  -- names someone thought of into the host's own member list - that is how
  -- editor:setCurrentTrack and editor:setCurrentGroup turned up *after* TRACKSEL
  -- had been reported as "no way to select a track in this build".  Enumeration is
  -- not guaranteed (Synthesizer V Studio 1 may not answer), so it is tried and the
  -- documented names stand in when it does not.
  local function keys_of(subject)
    if not subject then return nil end
    local ok, listing = pcall(function()
      local out = {}
      for key in pairs(subject) do out[#out + 1] = tostring(key) end
      return out
    end)
    if not ok or type(listing) ~= "table" or #listing == 0 then return nil end
    table.sort(listing)
    return listing
  end
  local function sample(subject, method)
    -- A group is a NoteGroupReference; step through getTarget() to reach the
    -- Note/NoteGroup methods, and step through a track to reach those.
    if not subject then return nil end
    local fn = member(subject, method)
    if fn ~= nil then return type(fn) end
    local gotTarget, target = attempt(function() return subject:getTarget() end)
    if gotTarget and target then
      fn = member(target, method)
      if fn ~= nil then return type(fn) end
    end
    local gotGroup, first = attempt(function() return subject:getGroupReference(1) end)
    if gotGroup and first then
      fn = member(first, method)
      if fn ~= nil then return type(fn) end
      local gotInner, inner = attempt(function() return first:getTarget() end)
      if gotInner and inner then
        fn = member(inner, method)
        if fn ~= nil then return type(fn) end
      end
    end
    local gotNote, note = attempt(function() return subject:getNote(1) end)
    if gotNote and note then
      fn = member(note, method)
      if fn ~= nil then return type(fn) end
    end
    return nil
  end

  local supported = {}
  local enumerated = {}
  for owner, subject in pairs(subjects) do
    -- The host's list when it has one, the manual's names otherwise, merged when
    -- both are known so a name from either source is reported.  Two keys are
    -- dropped because they are the host's own bookkeeping, not scripting API.
    local from_host = keys_of(subject)
    local seen, order = {}, {}
    local function add(name)
      if name and name ~= "__ptr__" and name ~= "isMemoryManaged" and not seen[name] then
        seen[name] = true
        order[#order + 1] = name
      end
    end
    for _, name in ipairs(documented[owner] or {}) do add(name) end
    if from_host then
      enumerated[#enumerated + 1] = owner
      for _, name in ipairs(from_host) do add(name) end
    end
    for _, method in ipairs(order) do
      -- The type comes back with the answer because the first live run turned
      -- into a one-row diagnosis: "userdata" in this column is what showed that
      -- Synthesizer V binds methods as callable userdata, not Lua functions.
      local kind = sample(subject, method)
      if kind then
        supported[#supported + 1] = owner .. ":" .. method .. "=" .. kind
      end
    end
  end
  if host then
    supported[#supported + 1] = "host:" .. tostring(host.hostName or "?")
    supported[#supported + 1] = "hostVersion:" .. tostring(host.hostVersion or "?")
    supported[#supported + 1] = "hostVersionNumber:" .. tostring(host.hostVersionNumber or "?")
  end
  -- Which owners were listed by the host itself rather than by this file's idea
  -- of the API.  An empty list means nothing below is complete.
  if #enumerated > 0 then
    supported[#supported + 1] = "enumerated=" .. table.concat(enumerated, "/")
  end
  table.sort(supported)
  return true, table.concat(supported, ",")
end

-- ---------------------------------------------------------------- batch
local function run_batch(lines)
  local state = {group = 1, added = 0}
  -- The documented method is newUndoRecord(); the earlier name was never real.
  attempt(function() return sv_project():newUndoRecord() end)

  for _, line in ipairs(lines) do
    local args = words(line)
    local verb = args[1]
    if verb and verb ~= "END" then
      local handler = handlers[verb]
      if handler then
        table.remove(args, 1)
        -- pcall around each handler: a thrown error becomes a reported "err"
        -- line and the rest of the batch still runs.  Without this, one bad
        -- command would abort the batch before its RESULT line and the client
        -- would wait out its whole timeout for an answer that never comes.
        -- pcall directly rather than through attempt(): a handler returns its own
        -- (ok, payload) pair, and pcall's status is a third thing again.  Folding
        -- those together is what made every reply carry "true" as its payload
        -- instead of its data.
        local outcome = { pcall(handler, state, args) }
        if outcome[1] then
          reply(verb, outcome[2], outcome[3])
        else
          reply(verb, false, tostring(outcome[2]))
        end
      else
        reply(verb, false, "unknown verb")
      end
    end
  end
  -- SV:finish() is deliberately NOT called here.  The manual: it marks the script
  -- finished and stops all subsequent async callbacks - and this script exists to
  -- stay resident and answer the next batch.
  return state
end

function main()
  if not (io and io.open) then
    -- Without io.open there is no channel at all, so say so instead of throwing
    -- "attempt to call a nil value" from inside the poll loop.
    if SV and SV.showMessageBox then
      SV:showMessageBox("LHai Agent", "This host does not expose io.open, so the " ..
        "LH AI Studio agent cannot reach its command file.")
    end
    return
  end

  -- Lua cannot be reloaded inside a running host, so editing this file while the
  -- editor is open used to mean restarting the editor to pick the change up.
  -- Every instance claims CLAIM with its own stamp at start, and an instance
  -- that finds somebody else's stamp there retires: running the script again is
  -- then enough.  A dedicated file rather than a line in the result log, because
  -- the log only grows and the newest stamp would eventually scroll out of any
  -- window this could afford to read.
  local stamp = tostring(os.time())
  if os.clock then stamp = stamp .. "-" .. tostring(math.floor(os.clock() * 1000000)) end
  -- The clock is process-wide - identical for two scripts in one host process -
  -- so a random component is what actually keeps two stamps apart.
  if math.random then
    local rolled, roll = pcall(math.random, 1, 1000000000)
    if rolled then stamp = stamp .. "-" .. tostring(roll) end
  end
  local INSTANCE = stamp
  local retired = false

  local function claim_instance()
    local f = io.open(CLAIM, "w")
    if f then f:write(INSTANCE) f:close() end
  end

  local function superseded_by()
    local f = io.open(CLAIM, "r")
    if not f then return nil end
    local who = (f:read("*a") or ""):match("^%s*(.-)%s*$")
    f:close()
    if who and who ~= "" and who ~= INSTANCE then return who end
    return nil
  end

  claim_instance()
  log("# agent started " .. os.date("%H:%M:%S") .. " " .. INSTANCE)
  local function tick()
    local ok, err = pcall(function()
      local newer = superseded_by()
      if newer then
        log("# agent retired " .. INSTANCE .. " (superseded by " .. newer .. ")")
        retired = true
        return
      end

      local f = io.open(CMD, "r")
      if not f then return end
      local lines = {}
      for line in f:lines() do lines[#lines + 1] = line end
      f:close()
      os.remove(CMD)

      local token = "-"
      for _, line in ipairs(lines) do
        local found = line:match("^END%s+(%S+)")
        if found then token = found end
      end
      local state = run_batch(lines)
      log(string.format("RESULT %s ok added=%d group=%d %s", token, state.added or 0, state.group or 1, os.date("%H:%M:%S")))
    end)
    if not ok then reply("loop", false, tostring(err)) end
    if retired then
      -- Here finish() *is* right: staying resident is the whole job, and this
      -- instance has been replaced.
      attempt(function() return SV:finish() end)
      return
    end
    -- The manual's signature is setTimeout(timeOut, callback): timeout first.
    SV:setTimeout(250, tick)
  end
  tick()
end
'''

#: Kept for callers that only want the raw Lua text with a placeholder intact.
TEMPLATE = _TEMPLATE
