# The in-process engine bridges

`lhai/live` drives the editors **from outside**: UI Automation, keystrokes, and
memory reads. That is a fallback. This document is the real architecture — the one
OpenUtau's MCP bridge demonstrates, and the one every engine here should end up
with:

> a small assembly loaded **inside** the engine's own process, holding the
> engine's own object model, publishing an MCP server on loopback HTTP.

No keyboard. No mouse. No screen coordinates. The score is an object graph, and
the bridge is a thin shim over it — which is why OpenUtau's bridge is 146 KB while
reimplementing nothing.

## Why in-process is not a preference, it is a requirement

Measured, not assumed:

```
> new tssinger2.TSSingerCLI()          // CeVIO, from our own process
System.TypeInitializationException
   -> System.Exception: Authentication error.
      at tssinger2.TSSingerCLI..cctor()
```

CeVIO's song engine authenticates in its static constructor, and that
authentication only exists inside a licensed CeVIO process. A standalone
re-implementation is therefore blocked by design, and the only correct place for
the bridge is **inside the host**.

The same reasoning applies to every engine here: the object graph and its licence
state live in the process, so the bridge lives in the process.

## The contract

Every bridge, on every engine, speaks the same thing.

### Transport

* JSON-RPC 2.0 over HTTP/1.1 on **loopback only** (`HttpListener` bound to
  `http://127.0.0.1:<port>/`), `POST`, single request per call.
* MCP methods: `initialize`, `notifications/initialized`, `tools/list`,
  `tools/call`, `ping`.
* Response is `application/json` — no SSE requirement, so a plain HTTP client
  works.
* **The port walks upward.** Start at 43102, try 43103 … 43111, take the first
  one that binds. Never assume the port; read it from the boot file.

### Diagnostics

Written next to the bridge assembly, so a failed start is diagnosable without a
debugger:

```
<bridge>.boot.txt        one line per event, timestamped, last line names the endpoint
<bridge>.status.json     {bridge:{version,protocol,builtAgainst,target}, app:{...},
                          compatibility:{ok,level,reasons,capabilities},
                          tools:{implemented:[...]}, server:{endpoint,port,pid}, errors:[]}
```

`boot.txt` and `status.json` are diagnostics. An installer must never delete them.

### Version honesty

The status payload carries `bridge.builtAgainst` (the app baseline the bridge was
compiled against), `bridge.protocol` (the contract revision), and `app.version`
(read from the running host). A bridge that cannot confirm both reports
`compatibility.ok: false` with `reasons`, and a client **stops instead of trying
tools**.

### The tool vocabulary

Flat, engine-prefixed, so one client drives all four without special cases:

| group | tools |
|---|---|
| status | `<engine>_status`, `<engine>_project_info`, `<engine>_list_singers`, `<engine>_describe_singer` |
| read | `<engine>_read_snapshot`, `<engine>_read_notes`, `<engine>_read_pitch`, `<engine>_list_curves`, `<engine>_read_curve` |
| write | `<engine>_add_notes`, `<engine>_edit_notes`, `<engine>_delete_notes`, `<engine>_set_lyric`, `<engine>_set_phoneme_override`, `<engine>_set_pitch`, `<engine>_set_vibrato`, `<engine>_write_curve` |
| tracks | `<engine>_add_track`, `<engine>_remove_track`, `<engine>_set_track_singer`, `<engine>_edit_track` |
| transport | `<engine>_play`, `<engine>_stop`, `<engine>_seek` |
| history | `<engine>_undo`, `<engine>_redo`, `<engine>_history_state` |
| render | `<engine>_export_capabilities`, `<engine>_export_audio`, `<engine>_job_get`, `<engine>_job_cancel` |
| project | `<engine>_project_new`, `<engine>_project_open`, `<engine>_project_save`, `<engine>_project_save_as` |

Capability levels are per tool group — `full` / `partial` / `none` — so a client
degrades instead of failing. OpenUtau's own payload is the model:

```json
"capabilities": { "notes": "full", "pitch_points": "full", "vibrato": "full",
                  "phoneme_override": "full", "export_audio": "full" }
```

### Editing rules

* **One undo block per request.** A request that adds 40 notes is one Ctrl+Z.
* **Never save implicitly.** `read_*` and edit tools change the document in
  memory; only `project_save` writes it.
* **Never touch the user's file.** No implicit reload, no implicit overwrite.

---

## What each engine gets

| engine | host language | in-process route | bridge |
|---|---|---|---|
| **CeVIO AI** | .NET Framework 4.8 (WPF) | managed assembly; `tssinger2.TSSingerCLI` is a public managed class | `bridges/cevio` |
| **Synthesizer V** | C++ with an embedded Lua host | the host already runs our script **in-process** | `lhai/live/synthv_lua.py` |
| **VoiSona** | native x64 | injected DLL holding the object graph | `bridges/native` + `voisona` adapter |
| **VOCALOID6** | native x64 | injected DLL holding the object graph | `bridges/native` + `vocaloid` adapter |

### CeVIO — the engine is a managed library

CeVIO's song engine is not a black box. `CeVIO.Song.dll`
(`CeVIO.Song, Version=6.3.13.0`) publishes the whole model:

```
tssinger2.TSSingerCLI
  ClearScore()                                  AddNote(Note)            AddRest(Rest)
  AddTempoMark(double)                          AddBeatMark(Beat)        AddKeyMark(Mode, int)
  SetVoices(TSVoicesCLI)   UnsetVoices()       SetVolume/SetAlpha
  SetGlobalHuskyShift/SetGlobalTuneShift        SetLanguages(ja, en)     SetSamplingFrequency(uint)
  Synthesize(begin, end, cb(IntPtr,int), endCb) -> PCM
  Synthesize(begin, end, cb, endCb, timing[], c0[], logF0[], vibAmp[], vibFrq[], alpha[], husky[], updated)
  GenerateParameter(...)   GeneratePhonemeSequence(...)   GenerateTimingParameter(...)
  LoadScoreFromMusicXML(path) / SaveScoreToMusicXML(path)   Stop()   GetSamplingFrequency()

tssinger2.Note     Lyric, Pitch(Octave, Step), Duration, Accent, Breath,
                   SlurStart/SlurStop, Staccato, TieStart/TieStop, Syllabic
tssinger2.Pitch    Octave: uint + Step: C Db D Eb E F Gb G Ab A Bb B
tssinger2.Rest     Duration
tssinger2.TSVoicesCLI          Load("<voice>.tsnvoice")
tssinger2.TSSyllableSeparatorCLI  Load(dictDir, config) + Separate(text) -> string[]
tssinger2.TSNotifier / TSSynthConditionCLI
```

Note the `SetGlobalHuskyShift` / `SetAlpha` / `SetGlobalTuneShift` triple: these
are the **vocal-technique controls**, and they are programmatic. So is
`Synthesize` — the bridge can render audio without playing anything.

Clock: **960 per quarter note**; CeVIO's own projects carry one 4/4 measure of
pre-roll at clock 3840.

### Synthesizer V — already in-process

The Lua host runs our script inside the editor, so the in-process requirement is
already met. Lua has no sockets, so the MCP facade lives in Python
(`lhai/mcp`), with the same vocabulary mapped onto the v3 verbs. This is the one
engine where the split is forced by the language, not by design.

Both generations are supported — **Synthesizer V Studio** (1.x) and
**Synthesizer V Studio 2** (2.x). They are separate products with separate script
folders and separate Lua hosts, so the agent is installed into every generation
found, and each gets its **own command/result folder** (`ipc/sv1`, `ipc/sv2`).
One shared folder would let two running editors claim the same batch and apply it
twice. Which folder a batch goes to is decided by the running process, which is
why the process's *image path* is read rather than its image name: both
generations ship `synthv-studio.exe`.

Two API facts are load-bearing enough to repeat, because getting either wrong
produces a silent no-op rather than an error: **Lua indexes from 1** while this
project's wire protocol is 0-based (converted in one helper, `toLua`/`fromLua`),
and there is no `newUndoBlock` — the call is `Project.newUndoRecord()`. Playback
lives on `SV:getPlayback()` → `PlaybackControl`, not on the main editor view.

### VoiSona and VOCALOID6 — native, injected

No scripting API, so the bridge is a native DLL injected with the existing
plumbing (`lhai/live/inject.py`) that (a) hosts the shared MCP HTTP server and
(b) speaks to the engine's own note model. Both ship the same server; only the
adapter differs, and the adapter is where per-build structure discovery happens —
the job `live_mem_scan` exists for.

---

## Installing

Each engine has a self-contained install prompt in `bridges/<engine>/prompt.md`.
The shared rules:

* **Verify a hash before installing**, and stop if it does not match.
* **Never kill the host.** Ask the human to save and close; unsaved work is the
  one thing an installer can destroy.
* **Never rebuild blindly.** A self-built binary targeting a different runtime
  than the installed app will not load; the build scripts pin the target.
* **Report the version, the target framework, the endpoint and the tool count**,
  and stop if `compatibility.ok` is false.
