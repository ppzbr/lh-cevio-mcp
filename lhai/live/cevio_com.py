"""CeVIO AI's speech side through its own COM server.

CeVIO AI is the only one of the four that publishes a real automation interface:
``CeVIO.Talk.RemoteService2`` (registered per-user, x64).  The surface below was
read back from the live type library, not guessed - this build exposes:

``IServiceControl2V40``  HostVersion, InterfaceVersion, IsHostStarted,
                         StartHost(noWait), CloseHost(mode)
``ITalker2V40``          Cast, AvailableCasts (IStringArray2), Components
                         (ITalkerComponentArray2), Speak -> ISpeakingState2,
                         Stop, GetTextDuration, GetPhonemes, OutputWaveToFile,
                         Volume, Speed, Tone, ToneScale, Alpha
``ISpeakingState2``      IsSucceeded, IsCompleted, Wait, Wait_2(timeout)
``IStringArray2``        Length, At(i)     <- *not* a SAFEARRAY

Two consequences worth knowing:

* **casts are a custom array interface**, so ``list(talker.AvailableCasts)``
  raises "does not support enumeration" - you must read ``.Length`` and ``.At(i)``;
* **there is no pitch slider** in the talk API; the five parameters are volume,
  speed, tone, tone-scale and alpha.

``comtypes`` is used rather than ``win32com`` because it resolves those custom
interfaces from the typelib and gives typed objects back.  The talker is created
once and cached: on installs whose COM registration points at a launcher, every
``CoCreateInstance`` starts a process, and a tool that spawns a window per call
would be a bug.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = [
    "available",
    "casts",
    "params",
    "set_voice",
    "say",
    "wave",
    "stop",
    "state",
    "phonemes",
]

_SERVICE_IDS = (
    "CeVIO.Talk.RemoteService2.ServiceControl2V40",
    "CeVIO.Talk.RemoteService2.ServiceControl2",
)
_TALKER_IDS = (
    "CeVIO.Talk.RemoteService2.Talker2V40",
    "CeVIO.Talk.RemoteService2.Talker2",
)

#: The five writable talk parameters.  CeVIO's own sliders are 0-100 with 50 as
#: the neutral default; the COM type is ULONG and does not clamp for us.
VOICE_PARAMS: dict[str, tuple[int, int]] = {
    "volume": (0, 100),
    "speed": (0, 100),
    "tone": (0, 100),
    "tone_scale": (0, 100),
    "alpha": (0, 100),
}

#: COM property name per parameter (``tone_scale`` -> ``ToneScale``).
_ATTRS = {
    "volume": "Volume",
    "speed": "Speed",
    "tone": "Tone",
    "tone_scale": "ToneScale",
    "alpha": "Alpha",
}

_service: Any = None
_service_progid = ""
_talker: Any = None
_talker_progid = ""


def _create(progids: tuple[str, ...]) -> tuple[Any, str]:
    import comtypes.client

    errors = []
    for progid in progids:
        try:
            return comtypes.client.CreateObject(progid), progid
        except Exception as exc:  # noqa: BLE001 - registration differs per build
            errors.append(f"{progid}: {type(exc).__name__}: {exc}")
    raise RuntimeError("; ".join(errors) or "no CeVIO COM ProgID answered")


def _service_ctl(start: bool = True) -> tuple[Any, str]:
    global _service, _service_progid
    if _service is None:
        _service, _service_progid = _create(_SERVICE_IDS)
    if start:
        try:
            if not bool(_service.IsHostStarted):
                _service.StartHost(True)  # noWait=True: return the pid, do not block
        except Exception:  # noqa: BLE001 - already started, or no flag on this build
            pass
    return _service, _service_progid


def _voice(cast: str = "") -> tuple[Any, str]:
    global _talker, _talker_progid
    if _talker is None:
        _talker, _talker_progid = _create(_TALKER_IDS)
    if cast:
        _talker.Cast = cast
    return _talker, _talker_progid


def _strings(array: Any) -> list[str]:
    """Read a CeVIO ``IStringArray2`` (Length + At), tolerating SAFEARRAYs too."""
    if array is None:
        return []
    if hasattr(array, "Length") and hasattr(array, "At"):
        return [str(array.At(i)) for i in range(int(array.Length))]
    try:
        return [str(item) for item in array]
    except TypeError:
        return []


def _require_cast(talker: Any) -> str:
    """Refuse to synthesise without a cast.

    A CeVIO install with no talk voicebank answers ``AvailableCasts.Length == 0``
    and ``Cast`` is ``None``.  ``Speak``/``OutputWaveToFile`` on that state block
    instead of failing, so the check is not cosmetic: it is what keeps a tool
    call from hanging forever.
    """
    cast = str(getattr(talker, "Cast", "") or "")
    if cast and cast != "None":
        return cast
    available = _strings(talker.AvailableCasts)
    if not available:
        raise RuntimeError(
            "no talk cast is installed (AvailableCasts is empty) - this CeVIO install "
            "has song voices only; use the song path or install a talk voicebank"
        )
    raise RuntimeError(f"no cast selected; pick one of {available[:6]}")


def _components(array: Any) -> list[dict[str, Any]]:
    if array is None or not hasattr(array, "Length"):
        return []
    out = []
    for i in range(int(array.Length)):
        try:
            component = array.At(i)
            out.append(
                {
                    "id": str(getattr(component, "Id", "")),
                    "name": str(getattr(component, "Name", "")),
                    "value": int(getattr(component, "Value", 0)),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return out


def available() -> dict[str, Any]:
    """Is the talk server registered, is its host up, and what version is it?"""
    if os.name != "nt":
        return {"ok": False, "error": "Windows only"}
    try:
        service, progid = _service_ctl(start=False)
        host_version = ""
        interface_version = ""
        for attr, bucket in (("HostVersion", "host"), ("InterfaceVersion", "interface")):
            try:
                value = str(getattr(service, attr))
            except Exception:  # noqa: BLE001
                continue
            if bucket == "host":
                host_version = value
            else:
                interface_version = value
        talker, talker_progid = _voice()
        casts = _strings(talker.AvailableCasts)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "progid": progid,
        "talker_progid": talker_progid,
        "host_started": bool(getattr(service, "IsHostStarted", False)),
        "host_version": host_version,
        "interface_version": interface_version,
        "casts": len(casts),
        "channel": "com",
    }


def casts() -> dict[str, Any]:
    """Every installed talk cast, plus the style components each one carries."""
    try:
        _service_ctl()
        talker, progid = _voice()
        names = _strings(talker.AvailableCasts)
        styles: dict[str, list[dict[str, Any]]] = {}
        for name in names[:32]:
            try:
                talker.Cast = name
                styles[name] = _components(talker.Components)
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "progid": progid,
        "count": len(names),
        "casts": names,
        "styles": styles,
        "channel": "com",
    }


def params() -> dict[str, Any]:
    return {"ok": True, "params": {k: list(v) for k, v in VOICE_PARAMS.items()}, "channel": "com"}


def set_voice(
    cast: str = "",
    component: str = "",
    component_value: int | None = None,
    volume: int | None = None,
    speed: int | None = None,
    tone: int | None = None,
    tone_scale: int | None = None,
    alpha: int | None = None,
) -> dict[str, Any]:
    """Set cast, an optional style component value, and the five sliders."""
    try:
        _service_ctl()
        talker, progid = _voice(cast)
        applied: dict[str, Any] = {"progid": progid, "cast": str(talker.Cast)}
        if component:
            target = talker.Components.ByName(component)
            if component_value is not None:
                target.Value = max(0, min(100, int(component_value)))
            applied["component"] = {"name": str(target.Name), "value": int(target.Value)}
        for name, value in (
            ("volume", volume),
            ("speed", speed),
            ("tone", tone),
            ("tone_scale", tone_scale),
            ("alpha", alpha),
        ):
            if value is None:
                continue
            low, high = VOICE_PARAMS[name]
            setattr(talker, _ATTRS[name], max(low, min(high, int(value))))
            applied[name] = getattr(talker, _ATTRS[name])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **applied, "channel": "com"}


def say(text: str, cast: str = "", wait: bool = False, timeout: float = 300.0) -> dict[str, Any]:
    """Speak ``text``.  With ``wait``, block on the speaking state CeVIO returns."""
    if not text:
        return {"ok": False, "error": "text is empty"}
    try:
        _service_ctl()
        talker, progid = _voice(cast)
        _require_cast(talker)
        state = talker.Speak(str(text))
        duration = 0.0
        try:
            duration = float(talker.GetTextDuration(str(text)))
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    result: dict[str, Any] = {
        "ok": True,
        "progid": progid,
        "cast": str(talker.Cast),
        "duration": round(duration, 2),
        "channel": "com",
    }
    if wait and state is not None:
        try:
            state.Wait_2(max(1.0, timeout))
            result["completed"] = bool(getattr(state, "IsCompleted", True))
            result["succeeded"] = bool(getattr(state, "IsSucceeded", True))
        except Exception as exc:  # noqa: BLE001
            result["wait_error"] = f"{type(exc).__name__}: {exc}"
    return result


def wave(text: str, path: str, cast: str = "") -> dict[str, Any]:
    """Render ``text`` to a WAV file (the one thing CeVIO does headless)."""
    if not text:
        return {"ok": False, "error": "text is empty"}
    if not path:
        return {"ok": False, "error": "path is required"}
    try:
        _service_ctl()
        talker, progid = _voice(cast)
        _require_cast(talker)
        ok = bool(talker.OutputWaveToFile(str(text), str(path)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    exists = os.path.exists(path)
    return {
        "ok": ok and exists,
        "reported": ok,
        "progid": progid,
        "path": path,
        "bytes": os.path.getsize(path) if exists else 0,
        "channel": "com",
        "error": "" if exists else "CeVIO reported success but wrote no file",
    }


def stop() -> dict[str, Any]:
    try:
        talker, _ = _voice()
        talker.Stop()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "channel": "com"}


def phonemes(text: str) -> dict[str, Any]:
    """Phonemes and timing CeVIO would synthesise for ``text``."""
    if not text:
        return {"ok": False, "error": "text is empty"}
    try:
        _service_ctl()
        talker, _ = _voice()
        _require_cast(talker)
        array = talker.GetPhonemes(str(text))
        items = []
        for i in range(int(getattr(array, "Length", 0))):
            item = array.At(i)
            items.append(
                {
                    "phoneme": str(item.Phoneme),
                    "start": round(float(item.StartTime), 3),
                    "end": round(float(item.EndTime), 3),
                }
            )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "count": len(items), "phonemes": items, "channel": "com"}


def state() -> dict[str, Any]:
    """Current cast, style components and slider values, read back from COM."""
    try:
        talker, progid = _voice()
        payload: dict[str, Any] = {"progid": progid, "cast": str(talker.Cast)}
        payload["components"] = _components(talker.Components)
        for name, attr in _ATTRS.items():
            try:
                payload[name] = int(getattr(talker, attr))
            except Exception:  # noqa: BLE001
                continue
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, **payload, "channel": "com"}
