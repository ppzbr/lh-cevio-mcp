# lh-cevio-mcp

Programmatic control of **CeVIO AI's song engine** — singers, tempo, key, notes,
lyrics, pitch, parameters, MusicXML in and out, and rendering. No keyboard, no
mouse, no UI Automation, no screen coordinates.

It works by putting an MCP bridge **inside the running editor**: a small managed
assembly that holds `tssinger2.TSSingerCLI`, the same class the editor itself uses
to build a score, and serves it over loopback HTTP as JSON-RPC. A one-file Python
loader attaches it to an editor that is already open, without administrator rights
and without restarting anything.

```
MCP client ──HTTP/JSON-RPC──▶ bridge inside CeVIO AI ──▶ tssinger2.TSSingerCLI
            127.0.0.1:431xx        (in-process)              CeVIO.Song.dll
```

## Why it has to be in-process

Measured, not assumed — this is what happens when the engine is touched from any
other process:

```
> new tssinger2.TSSingerCLI()          // from our own process
System.TypeInitializationException
   -> System.Exception: Authentication error.
      at tssinger2.TSSingerCLI..cctor()
```

The engine authenticates in its static constructor, and that authentication only
exists inside a licensed CeVIO process. There is no standalone tool to write, and
no audio file to decode: the licence state lives in the host. So the bridge lives
in the host.

## Requirements

| | |
|---|---|
| OS | Windows x64 |
| Editor | CeVIO AI, installed and licensed. Verified against `CeVIO.Song 6.3.13.0` |
| Runtime | .NET Framework 4.8 — `csc.exe` ships with Windows, so building needs no SDK |
| Python | 3.10 or newer (Route A and the client) |
| Optional | `zig` on PATH, or `pip install ziglang`, only to rebuild the native loader |

Nothing is patched, replaced or hooked. CeVIO's own files are never modified and the
open project is never touched. Route A writes nothing into the install directory at
all.

## Install

Two routes, same bridge, same endpoint. Both start from a clone:

```powershell
git clone https://github.com/ppzbr/lh-cevio-mcp
cd lh-cevio-mcp
python -m pip install -e ".[mcp]"
```

**Route A — inject into a running editor (recommended).** No administrator rights,
no restart, nothing written under `Program Files`. CeVIO AI must already be running.

```powershell
python bridges/cevio/inject.py
```

**Route B — the .NET start-up hook.** For when the editor has to come up with the
bridge already attached. Needs an **elevated** PowerShell, because it copies the
assembly into the CeVIO AI directory.

```powershell
# elevated
powershell -ExecutionPolicy Bypass -File bridges/cevio/install.ps1
```

It verifies the bridge targets `net48` *before* copying anything, adds
`CeVIO AI (with bridge).cmd` plus a Start Menu shortcut beside the shipped one (the
shipped shortcut is not modified), then starts CeVIO through the new launcher and
waits up to 45 s for the endpoint. The launcher starts the same executable your own
shortcut does — `CeVIO AI.exe` when present, else a renamed `CeVIO AI.orig.exe` —
because an unlicensed process throws `Authentication error.` at the bridge.

Undo with `powershell -File bridges/cevio/uninstall.ps1` (close CeVIO first: Windows
will not delete a loaded DLL).

Then, either way:

```powershell
lhai-mcp            # or: lhai-mcp http
```

## Verify

```powershell
python -m pytest        # offline, no editor involved
```

And after attaching, the honest source of truth is the bridge itself:

```
bridge_status()     # identity, app baseline, and per-capability levels
bridge_tools()      # the 21 tool schemas as the bridge publishes them
bridge_call(name="cevio_engine", args={})   # names every engine call it made and how it went
```

The endpoint is `http://127.0.0.1:43102/mcp`‑ish: the port **walks upward** from
43102 to 43111 and the first one that binds is used, so read it from
`LhCeVioBridge.status.json` rather than assuming it.

## Drive it

Aggregate tools on the client side:

| tool | what it does |
|---|---|
| `bridge_list` | every loopback bridge endpoint that is answering |
| `bridge_status` | one bridge's version, app baseline and capability levels |
| `bridge_tools` | the tools a bridge publishes |
| `bridge_call` | call any bridge tool by name — the escape hatch |
| `bridge_inject` / `bridge_eject` | load/unload a native bridge in a running editor |

The song tools, all prefixed `cevio_`:

```
cevio_bridge_status   cevio_engine          cevio_list_singers    cevio_project_info
cevio_set_voice       cevio_set_languages   cevio_set_key         cevio_set_tempo
cevio_set_params      cevio_read_notes      cevio_add_notes       cevio_edit_notes
cevio_delete_notes    cevio_clear_score     cevio_set_lyric       cevio_set_pitch
cevio_import_musicxml cevio_export_musicxml cevio_synthesize      cevio_stop
cevio_pipeline_probe
```

The MCP server also exposes `cevio_say`, `cevio_casts`, `cevio_wave`, `cevio_voice`,
`cevio_state` and `cevio_phonemes` — those drive CeVIO's **talk** half through its
COM API. They are a different engine in a different process and have nothing to do
with singing. The song half is reachable only through the bridge, with `bridge_call`.

## What it can and cannot do

* **Score work is complete.** Voices, languages, key, tempo, parameters, notes,
  lyrics, pitch, and MusicXML both ways. Nothing here needs rendering.
* **Rendering is refused by default.** `cevio_synthesize` has produced real audio —
  one measured call on a freshly started editor gave 96,000 frames of 48 kHz mono,
  peak 32768, and the editor survived — and it has also **killed the editor** with a
  `NullReferenceException` inside `tssinger2.TSNotifier.Synthesized`. Across four
  measured renders, a completed render was harmless, a stalled one poisoned the
  engine, and the next one faulted. Nothing in the bridge can tell the two apart in
  advance, so the tool requires `force: true` and the default answer is no. See
  `prompt.md`, Step 7.
* **One version is verified.** `CeVIO.Song 6.3.13.0`. Other versions may work; the
  bridge reports `compatibility` rather than assuming, and a client should stop
  instead of trying tools when `compatibility.ok` is false.

## Layout

```
bridges/cevio/
  LhCeVioBridge.cs          the managed bridge: TSSingerCLI over loopback MCP
  LhCeVioBridge.dll         built, net48/x64 — its sha256 is in the build-info
  LhCeVioBridge.build-info.txt   target framework, baseline, both hashes
  native/LhCeVioLoader.cpp  the native loader: starts the CLR in a running editor
  native/LhCeVioLoader.dll  built
  build.ps1                 csc for the bridge, zig for the loader
  install.ps1               Route B (elevated)      uninstall.ps1   undo it
  inject.py                 Route A (no elevation)
lhai/live/                  process finding, injection, COM/UIA, the live channels
lhai/mcp/                   the MCP server, the song IR, MIDI and MusicXML
docs/mcp-engine-bridge.md   the contract every bridge in this family speaks
prompt.md                   the paste-ready install-and-drive prompt
tests/                      the offline suite
tools/make-manifest.py      regenerate MANIFEST.txt
```

`lhai/live` and `lhai/mcp` are the **shared client** of the LH AI Studio family: the
same package drives Synthesizer V, VoiSona and VOCALOID6, so its adapters for those
engines are present here too. Only CeVIO's bridge is installed by this repository;
the other engines report themselves absent until their own bridge is installed, and
the talk tools above are the only CeVIO surface that does not go through the bridge.

## Verification record

Measured on the machine the bridge was developed on:

* the bridge **running inside a real CeVIO AI**, attached to the live editor with
  Route A: `CeVIO.Song 6.3.13.0` loaded in-process, the endpoint came up,
  `tools/list` returned all 21 tools, and `cevio_list_singers` returned the licensed
  voicebank under `...\Configuration\VocalSource\Song\CSNV-ENF-FP5 (x64)\`;
* the CLR-hosting chain the loader depends on, rehearsed in throwaway hosts before it
  was pointed at the editor — both when the loader starts the runtime itself and when
  the runtime is already running (the editor's case), with the managed entry point
  actually executing in both;
* `build.ps1` end to end;
* one render, which wrote `cevio_render.wav`: 96,000 frames, 48 kHz, mono, 16-bit,
  peak 32768, RMS 18283, 95,760 of 96,000 samples non-zero, CeVIO still running
  afterwards — and not repeatable, which is why synthesis is off by default.

`MANIFEST.txt` carries the sha256 of every file here, and
`bridges/cevio/LhCeVioBridge.build-info.txt` records the sha256 of both binaries
with the exact framework and engine version they were built against. The test suite
checks that the two agree, so the binary you install is the one that was recorded
rather than one that was rebuilt and never re-measured.

## Licence

MIT. See `LICENSE`. The one thing worth knowing before you ship audio made with
this: the *voices* are CeVIO's, and their terms are CeVIO's, not this repository's.
This code only drives an editor you already licensed.
