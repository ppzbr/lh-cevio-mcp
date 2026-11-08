// LhCeVioBridge - an in-process MCP bridge for CeVIO AI's song engine.
//
// CeVIO AI's song half has no scripting API, but it is not a black box either:
// CeVIO.Song.dll publishes tssinger2.TSSingerCLI, the very class the editor uses
// to build a score and synthesize it. This assembly is a thin shim over that
// class, plus an MCP server on loopback HTTP - the same shape as OpenUtau's
// bridge, and for the same reason: a standalone tool cannot work, because
//
//     new tssinger2.TSSingerCLI() -> System.Exception: Authentication error.
//
// is thrown by the engine's static constructor outside a licensed CeVIO process.
// The bridge therefore runs *inside* CeVIO.
//
// Two facts about TSSingerCLI shape the whole design:
//
//   * it is an ADD-ONLY score builder - ClearScore / AddNote / AddRest, with no
//     enumeration API. So this bridge owns the authoritative note list and
//     drives the engine by clearing and replaying it. There is exactly one
//     source of truth and it is here.
//   * Synthesize() hands back raw PCM through a callback, so rendering needs
//     neither the GUI nor playback.
//
// Build:  powershell -File build.ps1        (csc, net48, x64)
// Load:   AppDomainManager at host start-up, or ExecuteInDefaultAppDomain on a
//         running host (see LhCeVioLoader.cpp).
// Contract: docs/mcp-engine-bridge.md

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using tssinger2;

namespace LhCeVio
{
    public static class Bridge
    {
        public const string Name = "LhCeVioBridge";
        public const string Version = "0.1.0";
        //: Contract revision. Bumped when a tool's request or reply changes shape.
        public const int Protocol = 1;
        //: The app baseline this was compiled against. Reported so a mismatch is
        //: visible instead of mysterious.
        public const string BuiltAgainst = "CeVIO AI / CeVIO.Song 6.3.13.0 / net48";

        private const int PortFirst = 43102;
        private const int PortLast = 43111;
        //: 960 clock per quarter note, the engine's own unit.
        private const int TicksPerQuarter = 960;

        // How long a render is given to run before whatever has arrived is written
        // out anyway. Only reached if the engine never signals completion.
        private const int RenderTimeoutMs = 30000;

        // ---------------------------------------------------------------- state
        private static readonly object Gate = new object();
        private static HttpListener _listener;
        private static int _port;
        private static string _dir;
        private static bool _running;

        private static TSSingerCLI _singer;
        private static TSVoicesCLI _voices;
        private static string _voicePath = "";
        private static string _engineError = "";
        private static readonly Score _score = new Score();
        private static int _revision;

        //: True while the engine holds a score loaded by cevio_import_musicxml.
        //  TSSingerCLI is add-only and cannot be enumerated, so the note list this
        //  bridge keeps cannot be rebuilt from the engine - and replaying the
        //  bridge's own (empty) model over the import would discard it without a
        //  word.  While this is set, model-owning calls are refused instead.
        private static bool _modelStale;

        // ------------------------------------------------------------ documents
        /// <summary>
        /// The bridge's authoritative score. TSSingerCLI has no read API, so the
        /// model lives here and is replayed into the engine on demand.
        /// </summary>
        private sealed class Score
        {
            public double Bpm = 120.0;
            public int BeatsPerBar = 4;
            public int BeatType = 4;
            public string Mode = "MAJOR";
            public int Fifths = 0;
            public readonly List<NoteRec> Notes = new List<NoteRec>();
            public double Volume = 0.0;
            public double Alpha = 0.55;
            public double Husky = 0.0;
            public double Tune = 0.0;

            public double EndTicks
            {
                get
                {
                    double t = 0;
                    foreach (var n in Notes) t += n.Duration;
                    return t;
                }
            }
        }

        private sealed class NoteRec
        {
            public bool Rest;
            public int Octave = 4;
            public string Step = "C";
            public string Lyric = "a";
            public int Duration = TicksPerQuarter;
            public bool Accent, Breath, Staccato, SlurStart, SlurStop, TieStart, TieStop;
            public string Syllabic = "SINGLE";
        }

        // ------------------------------------------------------------- entry pts
        /// <summary>
        /// Signature required by ICLRRuntimeHost::ExecuteInDefaultAppDomain -
        /// a static int taking one string. Used when the loader injects us into
        /// an already-running CeVIO.
        /// </summary>
        public static int Start(string arg)
        {
            Boot(arg);
            // Return without blocking: the injected thread must not be parked in
            // a listener loop that the host cannot join.
            return 0;
        }

        /// <summary>Start the bridge. Idempotent; returns immediately.</summary>
        public static void Boot(string arg = "")
        {
            lock (Gate)
            {
                if (_running) return;
                _running = true;
            }
            _dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? ".";

            // Each stage is isolated. The endpoint must come up even when the
            // engine does not: a client that cannot see *why* a bridge is useless
            // has to guess, and guessing about someone's editor is unacceptable.
            try { Log("pid=" + System.Diagnostics.Process.GetCurrentProcess().Id + " booting " + Version); }
            catch { }
            try { EnsureSinger(); }
            catch (Exception ex)
            {
                _engineError = ex is TypeInitializationException && ex.InnerException != null
                    ? ex.InnerException.GetType().Name + ": " + ex.InnerException.Message
                    : ex.GetType().Name + ": " + ex.Message;
                Log("engine unavailable: " + _engineError);
            }
            try { LogEngineCapabilities(); }
            catch (Exception ex) { Log("engine walkthrough failed: " + DescribeException(ex)); }
            try { StartServer(); }
            catch (Exception ex) { Log("server failed: " + ex.GetType().Name + ": " + ex.Message); }
            try { WriteStatus(); }
            catch { }
        }

        // ------------------------------------------------------------- the engine
        private static TSSingerCLI Singer()
        {
            if (_singer == null)
            {
                _singer = new TSSingerCLI();
                _singer.SetSamplingFrequency(44100);
            }
            return _singer;
        }

        private static void EnsureSinger()
        {
            // Construction is what decides whether the engine is available.
            // GetSamplingFrequency deliberately is not: inside CeVIO that one query
            // faults natively (SEHException, 0x80004005) even though the object was
            // built successfully, and letting a query the engine answers badly mark
            // the whole engine unavailable would have hidden the fact that it
            // constructs. The result is logged rather than acted on.
            var s = Singer();
            Log("TSSingerCLI constructed");
            try { Log("samplingFrequency=" + s.GetSamplingFrequency()); }
            catch (Exception ex) { Log("samplingFrequency query faulted: " + DescribeException(ex)); }
        }

        // ------------------------------------------------- the engine's native state
        /// <summary>
        /// One of TSSingerCLI's private native pointer fields, read-only.
        ///
        /// These are the engine's own C++ objects; the managed wrapper holds nothing
        /// but the pointers to them. Reading a field cannot fault, and on net48 a
        /// pointer-typed field comes back as a System.Reflection.Pointer (or null),
        /// so "is it set?" is observable without a debugger.
        ///
        /// It matters for exactly one field: <c>notifier</c>. Synthesize passes it to
        /// the engine, which dereferences it unchecked, so while it is null rendering
        /// is an access violation instead of a returned error.
        /// </summary>
        private static object NativeField(string name)
        {
            var field = typeof(TSSingerCLI).GetField(
                name, BindingFlags.Instance | BindingFlags.NonPublic);
            if (field == null) return null;
            try { return field.GetValue(Singer()); }
            catch { return null; }
        }

        private static bool NotifierReady() { return NativeField("notifier") != null; }

        /// <summary>
        /// Whether the engine has been initialised enough to be asked native questions.
        ///
        /// Not the notifier pointer - that is set from construction. A voice load is
        /// the point at which the engine starts answering the sampling-frequency query
        /// (see SamplingFrequency), so that is the signal used here.
        /// </summary>
        private static bool IsInitialised() { return _voicePath != ""; }

        /// <summary>
        /// The engine's sampling frequency, or why it cannot be asked yet.
        ///
        /// Asking an engine that has never had a voice loaded raises a native
        /// SEHException, and CeVIO's own handler turns that into an "Error Occurred"
        /// dialog in the editor - so it is not asked until there is a reason to
        /// believe the engine will answer.
        ///
        /// That reason cannot be the notifier pointer: measured on this machine, the
        /// pointer is set at construction, on a cold singer included, so it predicts
        /// nothing. What the walkthrough does show is <c>loadVoice</c> followed by a
        /// working <c>getRate</c>, so a voice load is used as the condition here and
        /// nothing is poked before it.
        /// </summary>
        private static object SamplingFrequency()
        {
            if (_voicePath == "") return "uninitialised (no voice loaded through this bridge)";
            try { return Singer().GetSamplingFrequency(); }
            catch (Exception ex) { return "unavailable: " + DescribeException(ex); }
        }

        /// <summary>
        /// What the engine looks like from the inside, read-only. Reported so a
        /// caller can see the state that rendering is decided against rather than
        /// taking the refusal on faith.
        /// </summary>
        private static Dictionary<string, object> EngineState()
        {
            var names = new[]
            {
                "singer", "condition", "notifier", "forcedParam",
                "analyzer", "score", "analyzed_score",
            };
            var native = new Dictionary<string, object>();
            foreach (var name in names)
                native[name] = NativeField(name) == null ? "null" : "set";

            object analyzed = NativeField("isScoreAnalyzed");

            return new Dictionary<string, object>
            {
                { "native_fields", native },
                // Named for what it is. The first version called this
                // "notifier_ready" and treated it as a readiness signal; it is not
                // one, and the name invited exactly that mistake.
                { "notifier_set", NotifierReady() },
                { "is_score_analyzed", analyzed is bool ? analyzed : (object)false },
                { "sampling_frequency", SamplingFrequency() },
                {
                    "note",
                    "notifier_set is the field CeVIO's Synthesize dereferences, but a "
                    + "set pointer does not mean a render will survive: it is set from "
                    + "construction, cold singer included. What was actually measured "
                    + "on this machine: a render that runs to completion produces real "
                    + "audio and leaves the engine healthy, while a render that stalls "
                    + "leaves it in a state where the *next* render faults in "
                    + "TSNotifier::Synthesized and takes CeVIO down with it. That is "
                    + "why cevio_synthesize refuses unless force is set. The score "
                    + "path, voices and MusicXML are unaffected by any of this."
                },
            };
        }

        // ------------------------------------------------------------ pipeline probe
        private const int StepTimeoutMs = 5000;

        // Rooted in fields so the GC cannot collect a callback the native engine
        // may still be holding a pointer to after the call has returned.
        private static TSSingerCLI.CallbackDelegateIpInt2 _sequenceCallback;
        private static TSSingerCLI.CallbackDelegateIpInt3 _timingCallback;

        /// <summary>
        /// Walk CeVIO's own analysis order and report what each step does.
        ///
        /// This began as the search for whatever created the notifier, and that search
        /// is closed: measured on this machine, the notifier is set from construction
        /// on a cold singer too, so no step below ever was its trigger. What the probe
        /// is still good for is the one open question - a render that stalls leaves the
        /// engine unable to render again - by running the analysis calls a render
        /// itself runs, one at a time, and reporting exactly where one stops. It is the
        /// only call in the bridge that touches engine entry points never observed to
        /// complete, so it is refused unless force is set.
        /// </summary>
        private static Dictionary<string, object> PipelineProbe(bool force)
        {
            if (!force)
                return Fail(
                    "cevio_pipeline_probe walks engine calls that have never been observed "
                    + "to fault, but a native fault would take the editor down and that "
                    + "cannot be ruled out from here. Pass force:true to run it.");

            if (_voicePath == "") return Fail("no voice loaded; call set_voice first");
            if (_score.Notes.Count == 0) return Fail("score is empty");

            var steps = new List<object>();
            var s = Singer();

            var sequenceDone = new ManualResetEventSlim(false);
            var timingDone = new ManualResetEventSlim(false);
            _sequenceCallback = new TSSingerCLI.CallbackDelegateIpInt2(
                delegate(IntPtr a, int b, IntPtr c, int d) { sequenceDone.Set(); });
            _timingCallback = new TSSingerCLI.CallbackDelegateIpInt3(
                delegate(IntPtr a, int b, IntPtr c, int d, IntPtr e, int f) { timingDone.Set(); });

            RunPipelineStep(steps, "GeneratePhonemeSequence", sequenceDone,
                delegate() { return s.GeneratePhonemeSequence(_sequenceCallback); });
            RunPipelineStep(steps, "GenerateTimingParameter", timingDone,
                delegate() { return s.GenerateTimingParameter(_timingCallback); });

            return Ok(
                "steps", steps,
                "sampling_frequency", SamplingFrequency(),
                "is_score_analyzed", NativeField("isScoreAnalyzed") ?? false,
                "note", "Each step reports whether the engine returned and whether its "
                    + "callback arrived. A step that returns but never calls back is the "
                    + "shape a stalled render has, so that is the column to read first. "
                    + "This is a diagnostic, not a fix: cevio_synthesize is the call that "
                    + "produces audio, and it stays refused by default.");
        }

        private static void RunPipelineStep(
            List<object> steps, string name, ManualResetEventSlim done, Func<bool> call)
        {
            done.Reset();
            var entry = new Dictionary<string, object> { { "step", name } };
            try
            {
                entry["returned"] = call();
                entry["callback"] = done.Wait(StepTimeoutMs) ? "completed" : "timed out";
            }
            catch (Exception ex)
            {
                entry["threw"] = DescribeException(ex);
            }
            entry["notifier_set"] = NotifierReady();
            steps.Add(entry);
            try { Log("pipeline: " + name + " -> " + JsonConvert.SerializeObject(entry)); }
            catch { }
        }

        /// <summary>
        /// Walks the engine's score-building calls in order and logs which ones
        /// succeed, so a failure names a call rather than the whole engine. Each
        /// step is isolated: the engine reports trouble as a native fault carrying
        /// no message, so one bad call must not hide the calls around it.
        /// </summary>
        private static void LogEngineCapabilities()        {
            try { Log("engine walkthrough: " + RunSingerSteps()); }
            catch (Exception ex) { Log("engine walkthrough failed: " + DescribeException(ex)); }
        }

        private static string RunSingerSteps()
        {
            var report = new StringBuilder();
            TSSingerCLI s;
            try { s = new TSSingerCLI(); report.Append("ctor=ok; "); }
            catch (Exception ex) { return report.Append("ctor=" + DescribeException(ex)).ToString(); }

            Step(report, "setRate", delegate() { s.SetSamplingFrequency(44100); });
            Step(report, "getRate", delegate() { report.Append("=>" + s.GetSamplingFrequency() + " "); });
            Step(report, "clearScore", delegate() { s.ClearScore(); });
            Step(report, "addTempo", delegate() { s.AddTempoMark(120.0); });
            Step(report, "addBeat", delegate() { s.AddBeatMark(new Beat(4, 4)); });
            Step(report, "addKey", delegate() { s.AddKeyMark(tssinger2.Mode.MAJOR, 0); });
            Step(report, "addRest", delegate() { s.AddRest(new Rest { Duration = 480 }); });
            Step(report, "addNote", delegate()
            {
                s.AddNote(new Note
                {
                    Lyric = "a",
                    Duration = TicksPerQuarter,
                    Pitch = new Pitch(4, (Pitch.Steps)Enum.Parse(typeof(Pitch.Steps), "C", true)),
                    Syllabic = Note.Syllabics.SINGLE,
                });
            });
            Step(report, "setVolume", delegate() { s.SetVolume(0.0); });
            Step(report, "setAlpha", delegate() { s.SetAlpha(0.55); });
            Step(report, "setTune", delegate() { s.SetGlobalTuneShift(0.0); });
            Step(report, "setHusky", delegate() { s.SetGlobalHuskyShift(0.0); });
            Step(report, "loadVoice", delegate()
            {
                var path = FirstVoice();
                if (path == null) { report.Append("(no .tsnvoice on disk) "); return; }
                var voices = new TSVoicesCLI();
                report.Append("(load=" + voices.Load(path) + ") ");
                s.SetVoices(voices);
            });
            Step(report, "getRate2", delegate() { report.Append("=>" + s.GetSamplingFrequency() + " "); });

            // SetLanguages is the one score-side call CeVIO's own editor makes that
            // this bridge never did, and the internal TSSingerCLI::LoadConfigs() sits
            // behind it. A few plausible argument shapes are tried on this throwaway
            // singer so the log says which ones the engine actually accepts, instead
            // of leaving it to guesswork.
            Step(report, "setLanguages", delegate()
            {
                string root = AppDomain.CurrentDomain.BaseDirectory;
                string song = Path.Combine(root, "Configuration", "Dictionary", "Song");
                var tries = new[]
                {
                    new[] { "en_US", song },
                    new[] { "en_US", Path.Combine(song, "en_US") },
                    new[] { "", song },
                    new[] { "ja_JP,en_US", song },
                    new[] { "en_US", @"Configuration\Dictionary\Song" },
                };
                foreach (var pair in tries)
                {
                    bool accepted;
                    try { accepted = s.SetLanguages(pair[0], pair[1]); }
                    catch (Exception ex)
                    {
                        report.Append("[" + pair[0] + "|" + Path.GetFileName(pair[1]) + "="
                            + DescribeException(ex) + "] ");
                        continue;
                    }
                    report.Append("[" + pair[0] + "|" + Path.GetFileName(pair[1]) + "=" + accepted + "] ");
                }
            });
            return report.ToString();
        }

        private static void Step(StringBuilder report, string name, Action body)
        {
            try { body(); report.Append(name + "=ok; "); }
            catch (Exception ex) { report.Append(name + "=" + DescribeException(ex) + "; "); }
        }

        /// <summary>First .tsnvoice on disk, for probing the voice path.</summary>
        private static string FirstVoice()
        {
            try
            {
                var baseDir = Path.GetDirectoryName(typeof(TSSingerCLI).Assembly.Location);
                var root = Path.Combine(baseDir ?? ".", "Configuration", "VocalSource", "Song");
                if (!Directory.Exists(root)) return null;
                var files = Directory.GetFiles(root, "*.tsnvoice", SearchOption.AllDirectories);
                return files.Length > 0 ? files[0] : null;
            }
            catch { return null; }
        }

        private static string DescribeException(Exception ex)
        {
            var text = ex.GetType().Name + " (0x" + ex.HResult.ToString("X8") + "): " + ex.Message;
            if (ex.InnerException != null) text += " | inner " + DescribeException(ex.InnerException);
            return text;
        }

        /// <summary>Load a voice. <paramref name="path"/> is a .tsnvoice file.</summary>
        private static void UseVoice(string path) { UseVoice(path, false); }

        private static void UseVoice(string path, bool reload)
        {
            if (string.IsNullOrEmpty(path)) return;
            if (path == _voicePath && !reload) return;
            if (!File.Exists(path)) throw new FileNotFoundException("no such voice", path);
            var v = new TSVoicesCLI();
            if (!v.Load(path)) throw new InvalidOperationException("TSVoicesCLI.Load returned false: " + path);
            Singer().SetVoices(v);
            _voices = v;
            _voicePath = path;
            Log("voice loaded " + path);
        }

        /// <summary>
        /// Clear the engine and replay the model into it. This is the only way to
        /// edit a TSSingerCLI score, because it exposes no enumeration.
        /// </summary>
        private static void Replay()
        {
            if (_modelStale)
                throw new InvalidOperationException(
                    "the engine holds a score that came from cevio_import_musicxml, and this "
                    + "bridge cannot read it back - TSSingerCLI is add-only and exposes no note "
                    + "enumeration. Replaying this bridge's own model over it would discard the "
                    + "import silently, so the call is refused. cevio_export_musicxml still "
                    + "exports the imported score as it stands; cevio_clear_score returns to "
                    + "model-owned scoring.");

            var s = Singer();
            s.ClearScore();
            foreach (var n in _score.Notes)
            {
                if (n.Rest)
                {
                    var r = new Rest { Duration = (uint)n.Duration };
                    s.AddRest(r);
                }
                else
                {
                    var note = new Note
                    {
                        Lyric = n.Lyric,
                        Duration = (uint)n.Duration,
                        Pitch = new Pitch((uint)n.Octave, ParseStep(n.Step)),
                        Accent = n.Accent,
                        Breath = n.Breath,
                        Staccato = n.Staccato,
                        SlurStart = n.SlurStart,
                        SlurStop = n.SlurStop,
                        TieStart = n.TieStart,
                        TieStop = n.TieStop,
                        Syllabic = ParseSyllabic(n.Syllabic),
                    };
                    s.AddNote(note);
                }
            }
            s.AddTempoMark(_score.Bpm);
            s.AddBeatMark(new Beat((uint)_score.BeatsPerBar, (uint)_score.BeatType));
            s.AddKeyMark(_score.Mode == "MINOR" ? tssinger2.Mode.MINOR : tssinger2.Mode.MAJOR, _score.Fifths);
            s.SetVolume(_score.Volume);
            s.SetAlpha(_score.Alpha);
            s.SetGlobalHuskyShift(_score.Husky);
            s.SetGlobalTuneShift(_score.Tune);
            _revision++;
        }

        private static Pitch.Steps ParseStep(string s)
        {
            return (Pitch.Steps)Enum.Parse(typeof(Pitch.Steps), s, true);
        }

        private static Note.Syllabics ParseSyllabic(string s)
        {
            return (Note.Syllabics)Enum.Parse(typeof(Note.Syllabics), s, true);
        }

        private static int MidiOf(int octave, string step)
        {
            int[] offsets = { 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11 };
            int idx = (int)ParseStep(step);
            return (octave + 1) * 12 + offsets[idx];
        }

        private static void FromMidi(int midi, out int octave, out string step)
        {
            midi = Math.Max(0, Math.Min(127, midi));
            octave = midi / 12 - 1;
            step = Enum.GetNames(typeof(Pitch.Steps))[midi % 12];
        }

        // -------------------------------------------------------------- rendering
        /// <summary>
        /// Render to a WAV. Synthesize() delivers raw PCM through a callback; the
        /// engine reports its own sampling frequency, and the only unknown is the
        /// sample encoding, so the caller states it (default 32-bit float).
        /// </summary>
        private static Dictionary<string, object> Render(
            double beginTicks, double endTicks, string outPath, string format, int channels, bool force)
        {
            if (_score.Notes.Count == 0) return Fail("score is empty");
            if (_voicePath == "") return Fail("no voice loaded; call set_voice with a .tsnvoice path");

            // Refused by default, and the reason is measured rather than assumed.
            //
            // Synthesize drives the engine's native TSNotifier and dereferences it
            // unchecked. Testing recorded both outcomes on this machine:
            //
            //   * one render completed and produced real audio - 96,000 frames at
            //     48 kHz, peak 32768, RMS 18283, 95,760 of 96,000 samples non-zero,
            //     with CeVIO still running afterwards;
            //   * other renders faulted in exactly the way the first build did, and
            //     took CeVIO down with no chance to save:
            //
            //       NullReferenceException
            //         <Module>.tssinger2.TSNotifier.Synthesized(TSNotifier*, std::vector<double>*)
            //
            // The notifier field cannot be used to tell the two apart: it is *set*
            // from construction on every singer, including a cold one, so a
            // non-null pointer predicts nothing. cevio_engine reports it anyway,
            // because seeing that is how this was learned.
            //
            // So the tool refuses and says what is known, rather than guessing in
            // either direction. Every other call in the bridge - the whole score
            // path, voices, MusicXML - is unaffected by any of it.
            if (!force)
                return Fail(
                    "synthesize is refused by default because it has terminated CeVIO. "
                    + "The engine's native notifier is dereferenced unchecked and a fault "
                    + "in it is native, so the editor dies with no chance to save: "
                    + "TSNotifier::Synthesized. It is not hopeless - the sequence that "
                    + "worked on this machine was a freshly started CeVIO, a voice loaded, "
                    + "then this call with no begin_ticks/end_ticks (which requested "
                    + "the score's span plus a second of tail and produced 96,000 frames "
                    + "of real audio with the editor still running). What has never worked "
                    + "is a second render after one that stalled, and a request that runs "
                    + "past the score's end - each of those leaves the engine unable to "
                    + "render again. Because the difference is not visible from outside "
                    + "(the notifier pointer is set even on a cold engine), the tool "
                    + "refuses rather than picks for you. Pass force:true to attempt it, "
                    + "expecting to lose the open project if it goes wrong; cevio_engine "
                    + "and cevio_pipeline_probe (also force-gated) are the diagnostics.");

            Log("render: notifier_set=" + NotifierReady() + " initialised=" + IsInitialised());

            var pcm = new MemoryStream();
            int frames = 0;
            int callbacks = 0;
            var gate = new object();

            // The engine hands PCM to synthesizedCallback on its own schedule and
            // signals the end through endCallback, so the buffer may still be filling
            // after Synthesize has returned. Reading it at that point captures
            // nothing at all - which is exactly what an empty render looked like:
            // ok, 0 frames, 0 bytes.
            var complete = new ManualResetEventSlim(false);

            TSSingerCLI.CallbackDelegateIpInt onData = (IntPtr data, int samples) =>
            {
                int bytes = samples * (format == "i16" ? 2 : 4);
                var buf = new byte[bytes];
                System.Runtime.InteropServices.Marshal.Copy(data, buf, 0, bytes);
                lock (gate)
                {
                    if (callbacks++ == 0) Log("render: first callback samples=" + samples);
                    pcm.Write(buf, 0, bytes);
                    frames += samples / Math.Max(1, channels);
                }
            };
            TSSingerCLI.CallbackDelegate onEnd = () => { complete.Set(); };

            // The default window is the one that produced audio end to end: the
            // score's own span plus a second of tail. For this score that asks for
            // 1.0 + 1.0 s and the engine filled exactly 96,000 frames (2.0 s) and
            // signalled completion. A wider request did not: 0..4.0 s produced no
            // callbacks at all, and 2.0..4.0 s produced 0.57 s and then stalled, so
            // the engine has an internal bound this does not try to out-guess. Note
            // that a stall is not a harmless outcome - see the warning on the result.
            // Passing begin_ticks/end_ticks is the caller's business; `complete`
            // reports whether the engine ever signalled the end.
            double secondsPerTick = 60.0 / _score.Bpm / TicksPerQuarter;
            double beginSec = Math.Max(0.0, beginTicks) * secondsPerTick;
            double endSec = endTicks > 0
                ? endTicks * secondsPerTick
                : _score.EndTicks * secondsPerTick + 1.0;
            if (endSec <= beginSec)
                return Fail(
                    "end_ticks must be greater than begin_ticks: this score runs to "
                    + _score.EndTicks + " ticks, and a range that ends at or before it "
                    + "starts produces nothing. Omit begin_ticks/end_ticks to render the "
                    + "whole score.");

            var started = DateTime.UtcNow;
            if (!Singer().Synthesize(beginSec, endSec, onData, onEnd))
                return Fail("TSSingerCLI.Synthesize returned false");

            // A synchronous engine has already set this and the wait returns at once;
            // an asynchronous one is given the time to finish.
            bool finished = complete.Wait(RenderTimeoutMs);
            lock (gate)
            {
                Log("render: callbacks=" + callbacks + " frames=" + frames + " elapsed="
                    + (int)(DateTime.UtcNow - started).TotalMilliseconds + "ms finished=" + finished);
            }

            int rate = 48000;
            string rateNote = null;
            try { rate = (int)Singer().GetSamplingFrequency(); }
            catch (Exception ex)
            {
                // A render that stalled can leave the engine unable to answer this.
                // The captured audio is still worth writing, so the observed rate is
                // used for the header and the failure is reported instead of losing
                // the buffer to another exception.
                rateNote = "GetSamplingFrequency failed after the render: " + DescribeException(ex);
            }
            var raw = pcm.ToArray();
            WriteWav(outPath, raw, rate, channels, format == "i16" ? 16 : 32);
            var result = new Dictionary<string, object>
            {
                { "ok", true },
                { "path", outPath },
                { "sampling_frequency", rate },
                { "channels", channels },
                { "sample_format", format },
                { "frames", frames },
                { "bytes", raw.Length },
                { "seconds", Math.Round(frames / (double)Math.Max(1, rate), 3) },
                // Whether the engine signalled the end, not whether audio arrived.
                { "complete", finished },
                { "notifier_set", NotifierReady() },
            };
            if (rateNote != null) result["rate_note"] = rateNote;
            if (!finished)
            {
                result["note"] =
                    "the engine did not signal completion within " + RenderTimeoutMs
                    + "ms, so what it had produced by then was written out anyway. "
                    + "`complete: false` is the important field here, and not only "
                    + "because the audio is short: measured on this machine, a render "
                    + "that stalls leaves the engine such that the *next* render can "
                    + "fault in TSNotifier::Synthesized and terminate CeVIO. Treat a "
                    + "false here as a reason to save any open project before "
                    + "rendering again.";
                result["warning"] =
                    "a stalled render can leave the engine unable to render again - see "
                    + "bridges/README.md. Consider restarting CeVIO before the next "
                    + "attempt.";
            }
            return result;
        }

        private static void WriteWav(string path, byte[] data, int rate, int channels, int bits)
        {
            // IEEE-float (format 3) for 32-bit, PCM (format 1) for 16-bit.
            int fmt = bits == 16 ? 1 : 3;
            int blockAlign = channels * bits / 8;
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path)));
            using (var fs = new FileStream(path, FileMode.Create, FileAccess.Write))
            using (var w = new BinaryWriter(fs))
            {
                w.Write(Encoding.ASCII.GetBytes("RIFF"));
                w.Write(36 + data.Length);
                w.Write(Encoding.ASCII.GetBytes("WAVE"));
                w.Write(Encoding.ASCII.GetBytes("fmt "));
                w.Write(16);
                w.Write((short)fmt);
                w.Write((short)channels);
                w.Write(rate);
                w.Write(rate * blockAlign);
                w.Write((short)blockAlign);
                w.Write((short)bits);
                w.Write(Encoding.ASCII.GetBytes("data"));
                w.Write(data.Length);
                w.Write(data);
            }
        }

        // ------------------------------------------------------------------ tools
        private static Dictionary<string, object> Ok(params object[] kv)
        {
            var d = new Dictionary<string, object> { { "ok", true } };
            for (int i = 0; i + 1 < kv.Length; i += 2) d[(string)kv[i]] = kv[i + 1];
            return d;
        }

        private static Dictionary<string, object> Fail(string message)
        {
            return new Dictionary<string, object> { { "ok", false }, { "error", message } };
        }

        private static object Dispatch(string tool, JObject a)
        {
            switch (tool)
            {
                case "cevio_bridge_status":
                    return BridgeStatus();

                case "cevio_project_info":
                    return Ok("bpm", _score.Bpm, "notes", _score.Notes.Count,
                              "revision", _revision, "voice", _voicePath,
                              "end_ticks", _score.EndTicks,
                              "sampling_frequency", SamplingFrequency());

                case "cevio_read_notes":
                    return _modelStale
                        ? Ok("notes", NotesPayload(), "stale", true, "note",
                             "the engine's score came from cevio_import_musicxml and cannot be read "
                             + "back, so this is the bridge's own model, which is empty. "
                             + "cevio_clear_score returns to model-owned scoring.")
                        : Ok("notes", NotesPayload());

                case "cevio_clear_score":
                    _score.Notes.Clear();
                    _modelStale = false;
                    Replay();
                    return Ok("notes", 0, "revision", _revision);

                case "cevio_add_notes":
                    return AddNotes(a["notes"] as JArray);

                case "cevio_edit_notes":
                    return EditNotes(a["notes"] as JArray);

                case "cevio_delete_notes":
                    return DeleteNotes(a["index"] as JArray ?? a["indices"] as JArray);

                case "cevio_set_lyric":
                    return SetField(a, "lyric", (rec, v) => rec.Lyric = v);

                case "cevio_set_pitch":
                    return SetPitch(a);

                case "cevio_set_tempo":
                    _score.Bpm = a.Value<double?>("bpm") ?? _score.Bpm;
                    Replay();
                    return Ok("bpm", _score.Bpm);

                case "cevio_set_key":
                    _score.Mode = (a.Value<string>("mode") ?? _score.Mode).ToUpperInvariant();
                    _score.Fifths = a.Value<int?>("fifths") ?? _score.Fifths;
                    Replay();
                    return Ok("mode", _score.Mode, "fifths", _score.Fifths);

                case "cevio_set_params":
                    if (a["volume"] != null) _score.Volume = a.Value<double>("volume");
                    if (a["alpha"] != null) _score.Alpha = a.Value<double>("alpha");
                    if (a["husky"] != null) _score.Husky = a.Value<double>("husky");
                    if (a["tune"] != null) _score.Tune = a.Value<double>("tune");
                    Replay();
                    return Ok("volume", _score.Volume, "alpha", _score.Alpha,
                              "husky", _score.Husky, "tune", _score.Tune);

                case "cevio_list_singers":
                    return Ok("singers", ListSingers());

                case "cevio_set_voice":
                    UseVoice(a.Value<string>("path") ?? "");
                    return Ok("voice", _voicePath);

                case "cevio_synthesize":
                    return Render(
                        a.Value<double?>("begin_ticks") ?? 0.0,
                        a.Value<double?>("end_ticks") ?? 0.0,
                        a.Value<string>("path") ?? Path.Combine(_dir, "lhai_cevio.wav"),
                        (a.Value<string>("format") ?? "f32").ToLowerInvariant(),
                        a.Value<int?>("channels") ?? 1,
                        a.Value<bool?>("force") ?? false);

                case "cevio_engine":
                    return EngineState();

                case "cevio_pipeline_probe":
                    return PipelineProbe(a.Value<bool?>("force") ?? false);

                case "cevio_set_languages":
                    {
                        string languages = a.Value<string>("languages") ?? "";
                        string configs = a.Value<string>("configs") ?? "";
                        bool ok = Singer().SetLanguages(languages, configs);
                        return ok
                            ? Ok("languages", languages, "configs", configs)
                            : Fail("TSSingerCLI.SetLanguages returned false");
                    }

                case "cevio_export_musicxml":
                    {
                        string p = a.Value<string>("path") ?? Path.Combine(_dir, "lhai_cevio.musicxml");
                        // A stale model means the engine holds an imported score, and that is
                        // what should be written out - not a replay of the empty model.
                        if (!_modelStale) Replay();
                        bool ok = Singer().SaveScoreToMusicXML(p);
                        return ok
                            ? Ok("path", p, "source", _modelStale ? "engine (imported score)" : "bridge model")
                            : Fail("SaveScoreToMusicXML returned false");
                    }

                case "cevio_import_musicxml":
                    {
                        string p = a.Value<string>("path") ?? "";
                        if (!File.Exists(p)) return Fail("no such file: " + p);
                        bool ok = Singer().LoadScoreFromMusicXML(p);
                        if (!ok) return Fail("LoadScoreFromMusicXML returned false");
                        // The engine now holds a score this bridge did not build and cannot
                        // read back, so the model is emptied and marked stale rather than left
                        // claiming notes the engine does not have.
                        _score.Notes.Clear();
                        _modelStale = true;
                        return Ok("path", p, "stale", true, "note",
                            "the engine's score is now the imported file. This bridge cannot read it "
                            + "back, so add_notes/edit_notes/set_* are refused until cevio_clear_score; "
                            + "cevio_export_musicxml exports the imported score as it stands.");
                    }

                case "cevio_stop":
                    Singer().Stop();
                    return Ok("stopped", true);

                default:
                    return Fail("unknown tool: " + tool);
            }
        }

        private static object AddNotes(JArray notes)
        {
            if (notes == null) return Fail("notes[] is required");
            var added = new List<Dictionary<string, object>>();
            foreach (var token in notes)
            {
                var o = token as JObject;
                if (o == null) continue;
                var rec = new NoteRec();
                if (o["rest"] != null && o.Value<bool>("rest"))
                {
                    rec.Rest = true;
                }
                else if (o["pitch"] != null)
                {
                    int oct; string step;
                    FromMidi(o.Value<int>("pitch"), out oct, out step);
                    rec.Octave = oct; rec.Step = step;
                }
                else
                {
                    rec.Octave = o.Value<int?>("octave") ?? 4;
                    rec.Step = o.Value<string>("step") ?? "C";
                }
                rec.Lyric = o.Value<string>("lyric") ?? "a";
                rec.Duration = o.Value<int?>("duration_ticks") ?? TicksPerQuarter;
                rec.Accent = o.Value<bool?>("accent") ?? false;
                rec.Breath = o.Value<bool?>("breath") ?? false;
                rec.Staccato = o.Value<bool?>("staccato") ?? false;
                rec.SlurStart = o.Value<bool?>("slur_start") ?? false;
                rec.SlurStop = o.Value<bool?>("slur_stop") ?? false;
                rec.TieStart = o.Value<bool?>("tie_start") ?? false;
                rec.TieStop = o.Value<bool?>("tie_stop") ?? false;
                rec.Syllabic = o.Value<string>("syllabic") ?? "SINGLE";
                _score.Notes.Add(rec);
                added.Add(new Dictionary<string, object>
                {
                    { "index", _score.Notes.Count - 1 },
                    { "pitch", rec.Rest ? (object)null : MidiOf(rec.Octave, rec.Step) },
                    { "lyric", rec.Lyric },
                    { "duration_ticks", rec.Duration },
                });
            }
            Replay();
            return Ok("added", added.Count, "notes", _score.Notes.Count, "revision", _revision,
                      "detail", added);
        }

        private static object EditNotes(JArray notes)
        {
            if (notes == null) return Fail("notes[] is required");
            int changed = 0;
            foreach (var token in notes)
            {
                var o = token as JObject;
                if (o == null || o["index"] == null) continue;
                int i = o.Value<int>("index");
                if (i < 0 || i >= _score.Notes.Count) continue;
                var rec = _score.Notes[i];
                if (o["lyric"] != null) rec.Lyric = o.Value<string>("lyric");
                if (o["duration_ticks"] != null) rec.Duration = o.Value<int>("duration_ticks");
                if (o["octave"] != null) rec.Octave = o.Value<int>("octave");
                if (o["step"] != null) rec.Step = o.Value<string>("step");
                if (o["pitch"] != null) { int oc; string st; FromMidi(o.Value<int>("pitch"), out oc, out st); rec.Octave = oc; rec.Step = st; }
                if (o["accent"] != null) rec.Accent = o.Value<bool>("accent");
                if (o["breath"] != null) rec.Breath = o.Value<bool>("breath");
                if (o["staccato"] != null) rec.Staccato = o.Value<bool>("staccato");
                changed++;
            }
            Replay();
            return Ok("changed", changed, "revision", _revision);
        }

        private static object DeleteNotes(JArray indices)
        {
            if (indices == null) return Fail("index[] is required");
            var sorted = new List<int>();
            foreach (var t in indices) sorted.Add(t.Value<int>());
            sorted.Sort();
            for (int k = sorted.Count - 1; k >= 0; k--)
            {
                int i = sorted[k];
                if (i >= 0 && i < _score.Notes.Count) _score.Notes.RemoveAt(i);
            }
            Replay();
            return Ok("notes", _score.Notes.Count, "revision", _revision);
        }

        private static object SetField(JObject a, string key, Action<NoteRec, string> set)
        {
            if (a["index"] == null || a[key] == null) return Fail("index and " + key + " are required");
            int i = a.Value<int>("index");
            if (i < 0 || i >= _score.Notes.Count) return Fail("index out of range");
            set(_score.Notes[i], a.Value<string>(key));
            Replay();
            return Ok("index", i, key, a.Value<string>(key), "revision", _revision);
        }

        private static object SetPitch(JObject a)
        {
            if (a["index"] == null) return Fail("index is required");
            int i = a.Value<int>("index");
            if (i < 0 || i >= _score.Notes.Count) return Fail("index out of range");
            var rec = _score.Notes[i];
            if (a["pitch"] != null) { int oc; string st; FromMidi(a.Value<int>("pitch"), out oc, out st); rec.Octave = oc; rec.Step = st; }
            if (a["octave"] != null) rec.Octave = a.Value<int>("octave");
            if (a["step"] != null) rec.Step = a.Value<string>("step");
            Replay();
            return Ok("index", i, "pitch", MidiOf(rec.Octave, rec.Step), "revision", _revision);
        }

        private static List<Dictionary<string, object>> NotesPayload()
        {
            var outList = new List<Dictionary<string, object>>();
            double cursor = 0;
            for (int i = 0; i < _score.Notes.Count; i++)
            {
                var n = _score.Notes[i];
                outList.Add(new Dictionary<string, object>
                {
                    { "index", i },
                    { "onset_ticks", cursor },
                    { "duration_ticks", n.Duration },
                    { "rest", n.Rest },
                    { "pitch", n.Rest ? (object)null : MidiOf(n.Octave, n.Step) },
                    { "octave", n.Octave },
                    { "step", n.Step },
                    { "lyric", n.Lyric },
                    { "accent", n.Accent },
                    { "breath", n.Breath },
                    { "staccato", n.Staccato },
                    { "slur_start", n.SlurStart },
                    { "slur_stop", n.SlurStop },
                    { "tie_start", n.TieStart },
                    { "tie_stop", n.TieStop },
                    { "syllabic", n.Syllabic },
                });
                cursor += n.Duration;
            }
            return outList;
        }

        /// <summary>
        /// The song voices CeVIO AI has installed, read from its own
        /// Configuration tree - the same layout the editor uses, so a name found
        /// here is directly usable with set_voice.
        /// </summary>
        private static List<Dictionary<string, object>> ListSingers()
        {
            var found = new List<Dictionary<string, object>>();
            string[] roots =
            {
                @"C:\Program Files\CeVIO\CeVIO AI\Configuration\VocalSource\Song",
                @"C:\Program Files (x86)\CeVIO\CeVIO AI\Configuration\VocalSource\Song",
            };
            foreach (var root in roots)
            {
                if (!Directory.Exists(root)) continue;
                foreach (var dir in Directory.GetDirectories(root))
                {
                    foreach (var voice in Directory.GetFiles(dir, "*.tsnvoice"))
                    {
                        found.Add(new Dictionary<string, object>
                        {
                            { "name", Path.GetFileNameWithoutExtension(voice) },
                            { "component", Path.GetFileName(voice) },
                            { "folder", Path.GetFileName(dir) },
                            { "path", voice },
                        });
                    }
                }
            }
            return found;
        }

        private static Dictionary<string, object> BridgeStatus()
        {
            var reasons = new List<string>();
            if (!string.IsNullOrEmpty(_engineError)) reasons.Add("engine unavailable: " + _engineError);
            string appVersion = "unknown";
            try
            {
                var core = Assembly.Load("CeVIO.Song");
                appVersion = core.GetName().Version.ToString();
            }
            catch (Exception ex) { reasons.Add("CeVIO.Song not loaded: " + ex.Message); }

            bool ok = reasons.Count == 0;

            // TSSingerCLI is an add-only score builder: it exposes no enumeration
            // and no curve API, so the honest level for those is reported rather
            // than faked.
            var capabilities = new Dictionary<string, object>();
            capabilities["notes"] = "full";
            capabilities["rests"] = "full";
            capabilities["lyric"] = "full";
            capabilities["pitch"] = "full";
            capabilities["accent_breath_staccato"] = "full";
            capabilities["slur_tie"] = "full";
            capabilities["tempo_key_beat"] = "full";
            capabilities["alpha_husky_tune"] = "full";
            // "partial", and the level has to carry that meaning rather than flatter
            // the engine. Rendering is real - one call produced 96,000 frames of true
            // 48 kHz audio (peak 32768, RMS 18283, 95,760 of 96,000 samples non-zero,
            // written to cevio_render.wav) with CeVIO still running afterwards, so
            // "none" would be a lie. It is not "full" either: a render that stalls
            // leaves the engine in a state where the next render faults in
            // TSNotifier::Synthesized and takes the editor down, and nothing readable
            // from outside predicts which kind of attempt is coming. cevio_synthesize
            // therefore refuses unless force is set, and this line says so. Every
            // other capability is measured and unaffected by it.
            capabilities["synthesize"] = "partial";
            capabilities["engine_state"] = "full";
            capabilities["musicxml"] = "full";
            capabilities["read_notes"] = "bridge-mirror";
            capabilities["pitch_curve"] = "none";
            capabilities["vibrato_curve"] = "none";
            capabilities["phoneme_override"] = "none";
            capabilities["project_io"] = "none";

            var compatibility = new Dictionary<string, object>();
            compatibility["ok"] = ok;
            compatibility["level"] = ok ? "full" : "none";
            compatibility["reasons"] = reasons;
            compatibility["capabilities"] = capabilities;

            var output = new Dictionary<string, object>();
            output["ok"] = true;
            output["version"] = Version;
            output["protocol"] = Protocol;
            output["builtAgainst"] = BuiltAgainst;
            output["target"] = "net48";
            output["app"] = new Dictionary<string, object> { { "CeVIO.Song", appVersion } };
            output["compatibility"] = compatibility;
            output["tools"] = new Dictionary<string, object> { { "implemented", ToolNames() } };
            output["score"] = new Dictionary<string, object>
            {
                { "notes", _score.Notes.Count }, { "revision", _revision },
                { "voice", _voicePath }, { "endpoint", Endpoint() },
            };
            return output;
        }

        private static readonly string[] Tools =
        {
            "cevio_bridge_status", "cevio_project_info", "cevio_read_notes", "cevio_clear_score",
            "cevio_add_notes", "cevio_edit_notes", "cevio_delete_notes", "cevio_set_lyric",
            "cevio_set_pitch", "cevio_set_tempo", "cevio_set_key", "cevio_set_params",
            "cevio_list_singers", "cevio_set_voice", "cevio_set_languages", "cevio_synthesize",
            "cevio_export_musicxml", "cevio_import_musicxml", "cevio_stop",
            "cevio_engine", "cevio_pipeline_probe",
        };

        private static string[] ToolNames() { return Tools; }
        private static string Endpoint() { return _port > 0 ? "http://127.0.0.1:" + _port + "/mcp" : ""; }

        // ------------------------------------------------------------ MCP server
        private static void StartServer()
        {
            for (int port = PortFirst; port <= PortLast; port++)
            {
                var listener = new HttpListener();
                // Explicit 127.0.0.1 (not +/*) so no URL ACL is required and the
                // socket is unreachable from outside this machine.
                listener.Prefixes.Add("http://127.0.0.1:" + port + "/");
                try { listener.Start(); }
                catch (HttpListenerException) { try { listener.Close(); } catch { } continue; }

                _listener = listener;
                _port = port;
                Log("started http://127.0.0.1:" + port + "/mcp");
                var thread = new Thread(Loop) { IsBackground = true, Name = "lhai-mcp" };
                thread.Start();
                return;
            }
            throw new InvalidOperationException(
                "no free port in " + PortFirst + ".." + PortLast);
        }

        private static void Loop()
        {
            while (_listener != null && _listener.IsListening)
            {
                HttpListenerContext ctx;
                try { ctx = _listener.GetContext(); }
                catch (Exception) { break; }
                try { Handle(ctx); }
                catch (Exception ex) { Log("handler " + ex.GetType().Name + ": " + ex.Message); }
                finally { try { ctx.Response.Close(); } catch { } }
            }
        }

        private static void Handle(HttpListenerContext ctx)
        {
            string body;
            using (var r = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
                body = r.ReadToEnd();

            JObject req;
            try { req = JObject.Parse(body); }
            catch (Exception ex) { Write(ctx, Error(null, -32700, "parse error: " + ex.Message)); return; }

            var id = req["id"];
            string method = req.Value<string>("method");
            JObject parameters = req["params"] as JObject ?? new JObject();

            // A notification has no id and gets no body.
            if (id == null && method != null && method.StartsWith("notifications/")) return;

            try
            {
                switch (method)
                {
                    case "initialize":
                        Write(ctx, Result(id, new JObject
                        {
                            { "protocolVersion", "2024-11-05" },
                            { "capabilities", new JObject { { "tools", new JObject() } } },
                            { "serverInfo", new JObject
                                {
                                    { "name", Name }, { "version", Version },
                                    { "title", "CeVIO AI song bridge (in-process)" }
                                }
                            },
                        }));
                        return;

                    case "ping":
                        Write(ctx, Result(id, new JObject()));
                        return;

                    case "tools/list":
                        Write(ctx, Result(id, new JObject { { "tools", ToolList() } }));
                        return;

                    case "tools/call":
                        {
                            string tool = parameters.Value<string>("name");
                            var args = parameters["arguments"] as JObject ?? new JObject();
                            object outcome = Dispatch(tool, args);
                            Write(ctx, Result(id, new JObject
                            {
                                { "content", new JArray(new JObject
                                    {
                                        { "type", "text" },
                                        { "text", JsonConvert.SerializeObject(outcome, Formatting.Indented) },
                                    })
                                },
                                { "isError", false },
                            }));
                            return;
                        }

                    default:
                        Write(ctx, Error(id, -32601, "method not found: " + method));
                        return;
                }
            }
            catch (Exception ex)
            {
                var inner = ex is TargetInvocationException && ex.InnerException != null ? ex.InnerException : ex;
                Write(ctx, Result(id, new JObject
                {
                    { "content", new JArray(new JObject
                        {
                            { "type", "text" },
                            { "text", JsonConvert.SerializeObject(Fail(inner.GetType().Name + ": " + inner.Message)) },
                        })
                    },
                    { "isError", true },
                }));
            }
        }

        private static JArray ToolList()
        {
            var arr = new JArray();
            foreach (var name in Tools)
            {
                arr.Add(new JObject
                {
                    { "name", name },
                    { "description", Describe(name) },
                    { "inputSchema", Schema(name) },
                });
            }
            return arr;
        }

        private static string Describe(string tool)
        {
            switch (tool)
            {
                case "cevio_bridge_status": return "Bridge version, contract protocol, app baseline and per-capability compatibility levels.";
                case "cevio_project_info": return "Current bridge score: bpm, note count, revision, loaded voice, engine sampling frequency.";
                case "cevio_read_notes": return "Every note in the score, with onset, duration, pitch, lyric and the articulation flags.";
                case "cevio_clear_score": return "Remove every note and rest.";
                case "cevio_add_notes": return "Append notes and/or rests. Each entry takes pitch (MIDI) or octave+step, lyric, duration_ticks, and accent/breath/staccato/slur/tie flags.";
                case "cevio_edit_notes": return "Edit existing notes by index.";
                case "cevio_delete_notes": return "Delete notes by index list.";
                case "cevio_set_lyric": return "Set one note's lyric.";
                case "cevio_set_pitch": return "Set one note's pitch, as MIDI or octave+step.";
                case "cevio_set_tempo": return "Set the tempo mark.";
                case "cevio_set_key": return "Set key signature: mode MAJOR/MINOR and fifths.";
                case "cevio_set_params": return "Global vocal controls: volume, alpha, husky, tune.";
                case "cevio_list_singers": return "The song voices installed for CeVIO AI, with the .tsnvoice path to pass to set_voice.";
                case "cevio_set_voice": return "Load a .tsnvoice voice into the engine.";
                case "cevio_set_languages": return "Give the engine its lyric language list and dictionary config path (TSSingerCLI.SetLanguages).";
                case "cevio_synthesize": return "Render the score to a WAV through the engine's own callback. Produces real audio, and REFUSED unless force:true: rendering has taken CeVIO down in TSNotifier::Synthesized, and nothing readable from outside predicts which attempt is which. Read bridges/README.md before forcing it.";
                case "cevio_engine": return "The engine's native state, read-only: which internal pointers exist, whether the notifier pointer is set, sampling frequency, whether the score has been analysed, and why rendering is refused by default.";
                case "cevio_pipeline_probe": return "Diagnostic: run the engine's own analysis calls one at a time and report where each one stops. Refused unless force:true - these are the only engine entry points never observed to complete.";
                case "cevio_export_musicxml": return "Write the score as MusicXML via the engine's own exporter.";
                case "cevio_import_musicxml": return "Load a score from MusicXML into the engine. This bridge cannot read the engine's score back, so it marks its own model stale: edits are then refused until clear_score, and export writes the imported score.";
                case "cevio_stop": return "Stop any running synthesis.";
                default: return tool;
            }
        }

        private static JObject Schema(string tool)
        {
            var props = new JObject();
            var required = new JArray();
            switch (tool)
            {
                case "cevio_add_notes":
                    props["notes"] = new JObject { { "type", "array" }, { "description", "note objects" } };
                    required.Add("notes");
                    break;
                case "cevio_edit_notes":
                    props["notes"] = new JObject { { "type", "array" } };
                    required.Add("notes");
                    break;
                case "cevio_delete_notes":
                    props["index"] = new JObject { { "type", "array" }, { "items", new JObject { { "type", "integer" } } } };
                    required.Add("index");
                    break;
                case "cevio_set_lyric":
                    props["index"] = new JObject { { "type", "integer" } };
                    props["lyric"] = new JObject { { "type", "string" } };
                    required.Add("index"); required.Add("lyric");
                    break;
                case "cevio_set_pitch":
                    props["index"] = new JObject { { "type", "integer" } };
                    props["pitch"] = new JObject { { "type", "integer" } };
                    required.Add("index");
                    break;
                case "cevio_set_tempo":
                    props["bpm"] = new JObject { { "type", "number" } };
                    required.Add("bpm");
                    break;
                case "cevio_set_key":
                    props["mode"] = new JObject { { "type", "string" }, { "enum", new JArray("MAJOR", "MINOR") } };
                    props["fifths"] = new JObject { { "type", "integer" } };
                    break;
                case "cevio_set_params":
                    foreach (var k in new[] { "volume", "alpha", "husky", "tune" })
                        props[k] = new JObject { { "type", "number" } };
                    break;
                case "cevio_set_voice":
                    props["path"] = new JObject { { "type", "string" }, { "description", "path to a .tsnvoice" } };
                    required.Add("path");
                    break;
                case "cevio_synthesize":
                    props["path"] = new JObject { { "type", "string" } };
                    props["begin_ticks"] = new JObject { { "type", "number" } };
                    props["end_ticks"] = new JObject { { "type", "number" } };
                    props["format"] = new JObject { { "type", "string" }, { "enum", new JArray("f32", "i16") } };
                    props["channels"] = new JObject { { "type", "integer" } };
                    props["force"] = new JObject
                    {
                        { "type", "boolean" },
                        { "description", "attempt the render even though it has terminated CeVIO before and nothing readable from outside predicts a safe attempt. Save any open project first." },
                    };
                    break;
                case "cevio_pipeline_probe":
                    props["force"] = new JObject
                    {
                        { "type", "boolean" },
                        { "description", "run engine analysis steps that cannot be proven safe from outside; a native fault would close CeVIO" },
                    };
                    break;
                case "cevio_set_languages":
                    props["languages"] = new JObject { { "type", "string" } };
                    props["configs"] = new JObject { { "type", "string" } };
                    required.Add("languages");
                    required.Add("configs");
                    break;
                case "cevio_export_musicxml":
                case "cevio_import_musicxml":
                    props["path"] = new JObject { { "type", "string" } };
                    required.Add("path");
                    break;
            }
            return new JObject
            {
                { "type", "object" },
                { "properties", props },
                { "required", required },
            };
        }

        private static JObject Result(JToken id, JObject result)
        {
            return new JObject { { "jsonrpc", "2.0" }, { "id", id }, { "result", result } };
        }

        private static JObject Error(JToken id, int code, string message)
        {
            return new JObject
            {
                { "jsonrpc", "2.0" }, { "id", id },
                { "error", new JObject { { "code", code }, { "message", message } } },
            };
        }

        private static void Write(HttpListenerContext ctx, JObject payload)
        {
            var bytes = Encoding.UTF8.GetBytes(payload.ToString(Formatting.None));
            ctx.Response.StatusCode = 200;
            ctx.Response.ContentType = "application/json; charset=utf-8";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
        }

        // ------------------------------------------------------------ diagnostics
        private static void Log(string line)
        {
            try
            {
                string dir = _dir ?? Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                File.AppendAllText(Path.Combine(dir, Name + ".boot.txt"),
                    DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture) + " " + line + "\n",
                    Encoding.UTF8);
            }
            catch { }
        }

        private static void WriteStatus(string fatal = null)
        {
            try
            {
                var bridge = new JObject
                {
                    { "version", Version }, { "protocol", Protocol },
                    { "builtAgainst", BuiltAgainst }, { "target", "net48" }
                };
                var server = new JObject
                {
                    { "endpoint", Endpoint() }, { "port", _port },
                    { "pid", System.Diagnostics.Process.GetCurrentProcess().Id }
                };
                var score = new JObject
                {
                    { "notes", _score.Notes.Count }, { "revision", _revision },
                    { "voice", _voicePath }, { "bpm", _score.Bpm }
                };
                var status = new JObject
                {
                    { "bridge", bridge },
                    { "server", server },
                    { "score", score },
                    { "tools", new JObject { { "implemented", new JArray(Tools) } } },
                };
                var errors = new JArray();
                if (!string.IsNullOrEmpty(_engineError)) errors.Add(_engineError);
                if (fatal != null) errors.Add(fatal);
                status["errors"] = errors;
                File.WriteAllText(
                    Path.Combine(_dir, Name + ".status.json"),
                    status.ToString(Formatting.Indented), Encoding.UTF8);
            }
            catch { }
        }
    }

    /// <summary>
    /// Start-up hook for the AppDomainManager route: set
    /// APPDOMAIN_MANAGER_ASM=LhCeVioBridge and APPDOMAIN_MANAGER_TYPE=LhCeVio.DomainManager
    /// and the CLR hands us the domain before the application runs.
    ///
    /// It boots on a background thread, after a short delay, on purpose. The
    /// engine's static constructor is not safe to touch while the domain is still
    /// being created, and - more importantly - nothing this class does may throw:
    /// a failing start-up hook would stop the editor from launching at all. Boot()
    /// already swallows its own errors and writes them to boot.txt.
    /// </summary>
    public sealed class DomainManager : AppDomainManager
    {
        public override void InitializeNewDomain(AppDomainSetup setup)
        {
            try
            {
                var thread = new Thread(() =>
                {
                    Thread.Sleep(3000);
                    try { Bridge.Boot("appdomain-manager"); }
                    catch { }
                });
                thread.IsBackground = true;
                thread.Name = "lhai-cevio-boot";
                thread.Start();
            }
            catch { }
        }
    }
}
