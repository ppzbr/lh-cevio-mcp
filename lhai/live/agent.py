"""Client side of the in-process agent pipe.

The wire format is deliberately not JSON.  The agent is a small C++ DLL with no
dependencies, and a tab-separated request plus a length-prefixed response is
both trivial to write there and impossible to get subtly wrong::

    request   OK\\tREAD\\t7ff6a000\\t16\\r\\n
    response  OK 32\\r\\n<32 bytes of hex>

A response always starts with ``OK`` or ``ERR`` and a byte count, so a truncated
read is detected instead of silently returning half a struct.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from typing import Any

from lhai.live import inject as inject_mod

__all__ = ["pipe_path", "call", "ensure", "ping", "read_mem", "write_mem", "scan", "modules"]

BUFFER = 1 << 16


def pipe_path(pid: int) -> str:
    return rf"\\.\pipe\lhai_agent.{int(pid)}"


def _encode_request(command: str, *args: str) -> bytes:
    parts = [command, *[str(a) for a in args]]
    for part in parts:
        if "\t" in part or "\n" in part or "\r" in part:
            raise ValueError("agent arguments cannot contain tab or newline")
    return ("\t".join(parts) + "\r\n").encode("utf-8")


def _read_exact(handle: Any, count: int) -> bytes:
    import win32file

    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        try:
            _rc, data = win32file.ReadFile(handle, min(BUFFER, remaining))
        except Exception as exc:  # noqa: BLE001 - pipe closed under us
            raise ConnectionError(str(exc)) from exc
        if not data:
            raise ConnectionError("agent closed the pipe mid-response")
        chunks.append(data)
        remaining -= len(data)
    return b"".join(chunks)


def call(pid: int, command: str, *args: str, timeout: float = 5.0) -> dict[str, Any]:
    """Send one command.  Never raises: a dead pipe comes back as ``ok=False``."""
    if os.name != "nt":
        return {"ok": False, "error": "Windows only"}
    try:
        import pywintypes
        import win32file
        import win32pipe
    except ImportError:  # pragma: no cover - pywin32 is a hard dependency here
        return {"ok": False, "error": "pywin32 missing (pip install pywin32)"}

    payload = _encode_request(command, *args)
    deadline = time.time() + max(0.1, timeout)
    handle = None
    while True:
        try:
            handle = win32file.CreateFile(
                pipe_path(pid),
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                None,
                win32file.OPEN_EXISTING,
                0,
                None,
            )
            break
        except pywintypes.error as exc:
            if time.time() >= deadline:
                return {"ok": False, "error": f"agent not reachable on {pipe_path(pid)} ({exc})", "pid": pid}
            time.sleep(0.1)

    try:
        win32file.WriteFile(handle, payload)
        header = b""
        while b"\r\n" not in header and len(header) < 64:
            _rc, chunk = win32file.ReadFile(handle, 1)
            if not chunk:
                return {"ok": False, "error": "agent closed the pipe", "pid": pid}
            header += chunk
        line = header.split(b"\r\n", 1)[0].decode("utf-8", "replace").strip()
        status, _, count_text = line.partition(" ")
        try:
            count = int(count_text or 0)
        except ValueError:
            return {"ok": False, "error": f"bad agent header {line!r}", "pid": pid}
        body = _read_exact(handle, count) if count else b""
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "pid": pid}
    finally:
        try:
            handle.Close()
        except Exception:  # noqa: BLE001
            pass

    text = body.decode("utf-8", "replace")
    if status == "OK":
        return {"ok": True, "pid": pid, "result": text, "command": command}
    return {"ok": False, "pid": pid, "error": text, "command": command}


def ping(pid: int) -> dict[str, Any]:
    result = call(pid, "PING")
    if result.get("ok"):
        info = {}
        for line in str(result["result"]).splitlines():
            key, _, value = line.partition("=")
            if key:
                info[key.strip()] = value.strip()
        result["info"] = info
    return result


def ensure(pid: int, auto_inject: bool = True, timeout: float = 6.0) -> dict[str, Any]:
    """Make sure an agent is answering for ``pid``, injecting it if allowed."""
    probe = ping(pid)
    if probe.get("ok"):
        return {"ok": True, "injected": False, "info": probe.get("info", {})}
    if not auto_inject:
        return {"ok": False, "injected": False, "error": probe.get("error", "agent not answering")}

    result = inject_mod.inject(pid)
    if not result.ok:
        return {"ok": False, "injected": False, "error": result.error, "pid": pid}

    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        probe = ping(pid)
        if probe.get("ok"):
            return {"ok": True, "injected": True, "info": probe.get("info", {}), "hmodule": result.hmodule}
        time.sleep(0.2)
    return {"ok": False, "injected": True, "error": "agent loaded but the pipe never answered", "pid": pid}


# --------------------------------------------------------------------------- #
# typed helpers
# --------------------------------------------------------------------------- #
def modules(pid: int) -> list[dict[str, Any]]:
    result = call(pid, "MODULES")
    if not result.get("ok"):
        return []
    out = []
    for line in str(result["result"]).splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            try:
                out.append({"base": int(parts[0], 16), "size": int(parts[1], 16), "name": parts[2]})
            except ValueError:
                continue
    return out


def read_mem(pid: int, address: int, size: int) -> dict[str, Any]:
    result = call(pid, "READ", f"{int(address):x}", str(int(size)))
    if result.get("ok"):
        try:
            result["data"] = bytes.fromhex(str(result["result"]).strip())
        except ValueError:
            result["ok"] = False
            result["error"] = "agent returned non-hex data"
    return result


def write_mem(pid: int, address: int, data: bytes) -> dict[str, Any]:
    return call(pid, "WRITE", f"{int(address):x}", bytes(data).hex())


def scan(pid: int, pattern: str, module: str = "") -> dict[str, Any]:
    result = call(pid, "SCAN", module or "*", pattern)
    if result.get("ok"):
        hits = []
        for line in str(result["result"]).splitlines():
            line = line.strip()
            if line:
                try:
                    hits.append(int(line, 16))
                except ValueError:
                    continue
        result["hits"] = hits
    return result


def _main(argv: list[str]) -> int:  # pragma: no cover - debugging aid
    if len(argv) < 3:
        print("usage: python -m lhai.live.agent <pid> <command> [args...]")
        return 2
    pid = int(argv[1])
    out = call(pid, argv[2], *argv[3:])
    for key, value in out.items():
        print(f"{key}: {value}")
    return 0 if out.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv))
