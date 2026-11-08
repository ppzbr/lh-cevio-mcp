"""Attach the in-process CeVIO bridge to a CeVIO AI that is already running.

    python bridges/cevio/inject.py            # attach
    python bridges/cevio/inject.py --eject    # detach

This is the administrator-free route.  The AppDomainManager install has to put
the managed assembly inside CeVIO's own folder - which lives under Program Files,
so it needs elevation - and it only takes effect the next time CeVIO starts.  This
script instead injects ``LhCeVioLoader.dll`` into the running editor; the loader
starts the CLR inside the process and asks it to run ``LhCeVio.Bridge.Start`` from
the bridge assembly at an absolute path.

Nothing is written to the CeVIO installation, no file of the editor is modified,
and the editor does not need to be restarted.  The bridge only reads the engine's
object model and serves MCP on loopback; it does not touch the open project.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from lhai.live import apps, bridges, inject  # noqa: E402

HERE = Path(__file__).resolve().parent
BRIDGE_DLL = HERE / "LhCeVioBridge.dll"
LOADER_DLL = HERE / "native" / "LhCeVioLoader.dll"
TARGET_HINT = LOADER_DLL.with_name("LhCeVioLoader.target.txt")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def attach(timeout: float = 25.0) -> int:
    live = apps.find("cevio")
    if not live.running:
        print("CeVIO AI is not running - start it first")
        return 2

    for path in (BRIDGE_DLL, LOADER_DLL):
        if not path.exists():
            print(f"{path} is missing - build it: powershell -File bridges/cevio/build.ps1")
            return 2

    # The loader reads this to know which managed assembly to run.  It is written
    # fresh every time so the loader never carries a path of its own.
    TARGET_HINT.write_text(str(BRIDGE_DLL), encoding="utf-8")

    print(f"target  : {live.label} pid={live.pid} exe={live.exe}")
    print(f"bridge  : {BRIDGE_DLL}")
    print(f"        sha256 {_sha256(BRIDGE_DLL)}")
    print(f"loader  : {LOADER_DLL}")
    print(f"        sha256 {_sha256(LOADER_DLL)}")

    if inject.is_loaded(live.pid, LOADER_DLL.name):
        print("loader already present; eject first if you rebuilt it")
    else:
        result = inject.inject(live.pid, dll=LOADER_DLL, wait=10.0)
        if not result.ok:
            print(f"injection failed: {result.error}")
            return 1
        print(f"injected: hmodule={result.hmodule:#x}")

    # The loader logs its own progress next to itself; surface it on failure.
    loader_log = LOADER_DLL.with_name("LhCeVioLoader.boot.txt")
    bridge_log = BRIDGE_DLL.with_name("LhCeVioBridge.boot.txt")

    deadline = time.time() + timeout
    entry = None
    while time.time() < deadline:
        entry = bridges.for_engine("cevio")
        if entry:
            break
        time.sleep(0.5)

    if not entry:
        print("no endpoint appeared; the loader's log says how far it got:")
        if loader_log.exists():
            print(loader_log.read_text(encoding="utf-8", errors="replace").strip())
        else:
            print("  (no loader log - the DLL never ran)")
        if bridge_log.exists():
            print("--- bridge log ---")
            print(bridge_log.read_text(encoding="utf-8", errors="replace").strip())
        return 1

    print(f"endpoint: {entry['endpoint']}")
    status = bridges.status(entry["endpoint"])
    tools = bridges.tools(entry["endpoint"])
    print(f"engine  : {status.get('app', {}).get('CeVIO.Song', '?')}")
    print(f"tools   : {len(tools)}")
    for tool in tools:
        print(f"   {tool.get('name', '')}")
    compatibility = status.get("compatibility", {})
    print(f"compat  : ok={compatibility.get('ok')} level={compatibility.get('level')}")
    for reason in compatibility.get("reasons", []):
        print(f"   reason: {reason}")
    print(f"score   : {status.get('score')}")

    # The real test of the whole design: can the engine be instantiated inside
    # this process?  Outside CeVIO this is the call that answers
    # "Authentication error." - inside it, it is the proof.
    print("engine probe:")
    for tool, arguments in (
        ("cevio_list_singers", {}),
        ("cevio_project_info", {}),
    ):
        payload = bridges.call(entry["endpoint"], tool, arguments)
        print(f"   {tool} -> {str(payload)[:300]}")

    return 0 if compatibility.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="attach the CeVIO in-process bridge")
    parser.add_argument("--eject", action="store_true", help="detach the loader instead")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args(argv)
    if args.eject:
        print("the CeVIO loader has no unload export; close CeVIO to detach it")
        return 2
    return attach(timeout=args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
