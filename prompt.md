# CeVIO AI — install and drive the song engine

> Paste everything below the rule into an agent that has shell access to the Windows
> machine running CeVIO AI. It is written to stand alone: it names every file it
> needs, and the repository it starts from is the one that contains this file.

---

## Goal

Give a program — not a person — control of CeVIO AI's **song** engine: singers,
tempo, key, notes, lyrics, pitch, parameters, MusicXML in and out, and rendering.
Nothing may be clicked or typed.

**How it is done.** CeVIO AI's song engine publishes a managed class,
`tssinger2.TSSingerCLI`, in `CeVIO.Song.dll` — the same class the editor uses to
build a score and render audio. The bridge is a thin C# shim over that class plus an
MCP server on loopback HTTP, **injected into the running editor**. It has to run
inside CeVIO: the engine's static constructor throws `Authentication error.` in any
process CeVIO has not licensed, which is why no standalone tool can exist.

**What it does not do.** It does not patch, replace or modify any CeVIO file, it does
not hook anything, and it never touches the open project.

## Rules

1. Do not modify anything inside the CeVIO AI install directory. Route A below writes
   nothing there at all.
2. Do not rebuild the bridge unless the existing one is missing or fails its build
   check. A bridge built for the wrong runtime will not load, and the failure will not
   explain itself.
3. If a step fails, **stop at that step and report exactly what happened.** Do not
   work around it, do not try a different runtime, do not edit the source.
4. Never kill the editor to make a step succeed.
5. Work only inside this repository. It is the CeVIO bridge and the client that
   drives it; there is nothing else in it to touch.

## Step 0 — the repository and dependencies

```powershell
git clone https://github.com/ppzbr/lh-cevio-mcp
cd lh-cevio-mcp
python -m pip install -e ".[mcp]"
```

`comtypes` is part of the `[mcp]` extra, because `lhai/live/cevio_com.py` and
`lhai/live/uia.py` import it — one install is enough. Nothing else is needed to
attach the bridge: the injection path is `ctypes` only.

Start the MCP server (`lhai-mcp`, or `lhai-mcp http`) and call the tools below from
your client. With no MCP client, the same functions are importable directly.

## Step 1 — prove the checkout is intact

```powershell
python -m pytest
```

This runs offline, with no editor involved. Expected: every test passes. The suite
includes an integrity check of this repository — `MANIFEST.txt` against every file,
and the shipped `LhCeVioBridge.dll` against the sha256 in its build-info — so a pass
means the binary you are about to install is the one that was built and recorded.
If it fails, stop and report; do not reinstall or hand-edit a hash.

## Step 2 — check the prerequisite

```powershell
Test-Path 'C:\Program Files\CeVIO\CeVIO AI\CeVIO.Song.dll'
```

If that is false, CeVIO AI is not installed where expected. Stop and report. Read the
engine's own version and quote it in your report:

```powershell
[Reflection.AssemblyName]::GetAssemblyName('C:\Program Files\CeVIO\CeVIO AI\CeVIO.Song.dll').FullName
```

Expect something like `CeVIO.Song, Version=6.3.13.0`. The bridge was developed against
`6.3.13.0`; a different version is not automatically a problem, but say so.

## Step 3 — build the bridge

```powershell
powershell -ExecutionPolicy Bypass -File bridges/cevio/build.ps1
```

This needs .NET Framework's `csc.exe`, which ships with Windows. It writes
`LhCeVioBridge.dll` and `LhCeVioBridge.build-info.txt` into `bridges/cevio/`, and the
same directory also holds `native/LhCeVioLoader.dll`. Expected tail:

```
built: LhCeVioBridge, ...
sha256: <64 hex characters>
bytes : ...
wrote build-info.txt
```

If it fails, stop and report the compiler output verbatim. Rebuilding the native
loader (which also happens here) needs `zig` on PATH or `pip install ziglang`; the
repository already ships the loader it was verified with, so a failure there is not
grounds for editing anything.

## Step 4 — attach it

**Route A — inject. No administrator rights, no restart, nothing written into
`Program Files`.** CeVIO must already be running.

```powershell
python bridges/cevio/inject.py
```

This loads `bridges/cevio/native/LhCeVioLoader.dll` into the editor's own process; the
loader reaches the CLR the editor already started and runs `LhCeVio.Bridge.Start` out
of the bridge assembly by absolute path. If it prints `no endpoint appeared`, the
loader's log says how far it got:

```powershell
Get-Content bridges/cevio/native/LhCeVioLoader.boot.txt
```

**Route B — the .NET start-up hook.** Use this only when CeVIO has to come up with the
bridge already attached. It needs an **elevated** PowerShell, because it writes into
the CeVIO AI directory.

```powershell
powershell -ExecutionPolicy Bypass -File bridges/cevio/install.ps1
```

The script verifies the bridge targets `net48` before copying it, copies the DLL and
its build info into the CeVIO AI directory, writes `CeVIO AI (with bridge).cmd` and a
Start Menu shortcut beside the shipped one (**the shipped shortcut is not modified**),
then starts CeVIO through the new launcher and waits up to 45 s for the endpoint.

The launcher starts the same executable your own shortcut starts — `CeVIO AI.exe` if
it is there, otherwise a renamed `CeVIO AI.orig.exe`. That matters: an unlicensed
process throws `Authentication error.` at the bridge, so the launcher must not start
a different binary than the one you normally run.

If it reports that the start-up hook did not run (no `boot.txt` at all), the
`APPDOMAIN_MANAGER_*` variables did not reach the process. Report that; do not start
editing the launcher.

## Step 5 — confirm it worked

Expected shape:

```
endpoint : http://127.0.0.1:431<nn>/mcp
tools    : 21
   cevio_bridge_status
   cevio_project_info
   ...
```

The port walks upward from 43102, so read it from the output rather than assuming it.
If the endpoint came up but the tools are missing, read the diagnostics — they are
written next to the bridge DLL, so the path depends on the route:

```powershell
# Route A
Get-Content 'bridges/cevio/LhCeVioBridge.boot.txt'
Get-Content 'bridges/cevio/LhCeVioBridge.status.json' -Raw
# Route B
Get-Content 'C:\Program Files\CeVIO\CeVIO AI\LhCeVioBridge.boot.txt'
```

One boot-log line is expected and is **not** a fault:

```
samplingFrequency query faulted: SEHException (0x80004005)
```

`TSSingerCLI.GetSamplingFrequency()` faults natively until a voice is loaded and
answers normally after one is. The bridge therefore treats **a voice load** as its
health check. The `engine walkthrough:` line is the one to read; on a healthy install
it ends `... loadVoice=ok; getRate2=ok; ... setLanguages=ok`. `getRate` faulting
alongside `getRate2=ok` is the same story as above. **Anything else on that line is a
real failure** and should be reported verbatim — it names each call, so a break points
at a call rather than at the engine as a whole. `cevio_engine` reports `uninitialised`
rather than asking before a voice is loaded, deliberately: the same fault inside a tool
call puts CeVIO's "Error Occurred" dialog on screen.

## Step 6 — drive it

Reach the bridge through the MCP client's bridge tools:

```
bridge_list()                       # every loopback bridge endpoint that is listening
bridge_status()                     # identity, version, per-capability levels
bridge_tools()                      # the 21 tool schemas, as the bridge publishes them
bridge_call(name="cevio_project_info", args={})     # one call, one JSON answer
```

The song tools, all prefixed `cevio_`:

```
cevio_bridge_status   cevio_engine          cevio_list_singers    cevio_project_info
cevio_set_voice       cevio_set_languages   cevio_set_key         cevio_set_tempo
cevio_set_params      cevio_read_notes      cevio_add_notes       cevio_edit_notes
cevio_delete_notes    cevio_clear_score     cevio_set_lyric       cevio_set_pitch
cevio_import_musicxml cevio_export_musicxml cevio_synthesize      cevio_stop
cevio_pipeline_probe
```

**Do not confuse these with the talk tools.** The MCP server also exposes
`cevio_casts`, `cevio_say`, `cevio_wave`, `cevio_voice`, `cevio_state` and
`cevio_phonemes`. Those drive CeVIO's **talk** half through its COM API and have
nothing to do with singing. The singing half is only reachable through the injected
bridge, with `bridge_call`.

Read `cevio_bridge_status` / `cevio_engine` and trust the `capabilities` map they
return; it is the bridge's own honest statement of what this build can do.

## Step 7 — synthesis is refused by default, and that is deliberate

`cevio_synthesize` **produces real audio** — one call on a freshly started CeVIO
produced 96,000 frames of genuine 48 kHz mono, peak 32768, and the editor survived.

It has also **killed the editor** with no chance to save:

```
System.NullReferenceException
  <Module>.tssinger2.TSNotifier.Synthesized(tssinger2.TSNotifier*, std::vector<double>*)
```

Across four measured renders: a span plus one second on a fresh engine completed; a
second render on that same engine produced 27,360 frames and then stalled; a fresh
engine produced zero callbacks and stalled; and the render after a stall faulted
outright. So a completed render is harmless, a **stalled** render poisons the engine,
and the **next** render faults. A non-null notifier pointer predicts nothing — read on
a cold singer it is already set — so the bridge cannot tell a working attempt from a
fatal one in advance. That is why the tool refuses unless it is called with
`force: true`.

**Default behaviour: do not call `cevio_synthesize` with `force: true`.** Everything
else — score construction, notes, lyrics, pitch, parameters, MusicXML, saving from the
editor — works without rendering. Only attempt a forced render if the human asks for
it, on a freshly started editor, and say in advance that the editor may die.

## Step 8 — when it goes wrong

| symptom | cause |
|---|---|
| `Authentication error.` | the engine is being touched from outside a licensed CeVIO process — the bridge must run *inside* the editor (Route A or B) |
| `no endpoint appeared` | read `LhCeVioLoader.boot.txt`; report the last line |
| endpoint up, no tools | read `LhCeVioBridge.boot.txt` and `LhCeVioBridge.status.json` |
| the DLL will not be overwritten | a loaded bridge DLL cannot be replaced while CeVIO runs — ask the human to close CeVIO, or use Route A which loads from the repository and writes nothing into the install directory |
| "Error Occurred at v9.1.14.0" | a tool call faulted in the engine's native layer — say which call preceded it and stop |

If a step fails, stop and report. Never restart, or kill, an editor that holds
unsaved work.
