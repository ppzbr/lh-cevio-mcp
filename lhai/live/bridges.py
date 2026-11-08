"""Discovery and proxying for the in-process engine bridges.

The bridges in ``bridges/`` are what actually touch a *live* engine: a small
assembly loaded inside the editor's own process, holding that editor's object
model, publishing MCP on loopback HTTP.  This module is the client side - it
finds the endpoints that exist right now and forwards calls to them.

There is deliberately no registry of ports.  A bridge binds the first free port
in a fixed window, so the only reliable way to find it is to knock: send
``initialize`` to each candidate and read back ``serverInfo``.  That also
identifies *which* bridge answered, which matters because OpenUtau's bridge uses
the same window.

Nothing here injects, launches or modifies anything; it is read-mostly
plumbing over an endpoint the user already started.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

__all__ = [
    "PORT_FIRST",
    "PORT_LAST",
    "endpoints",
    "discover",
    "call",
    "tools",
    "status",
    "for_engine",
    "tool_prefix",
    "native_bridge_dll",
    "EJECT_EXPORT",
]

#: Export the native bridge publishes so it can unload itself on request.
EJECT_EXPORT = "LhaiBridgeUnload"


def native_bridge_dll() -> Path:
    """The built native bridge DLL, beside this package's repository root."""
    return Path(__file__).resolve().parents[2] / "bridges" / "native" / "lhai_bridge.dll"

#: The shared bridge port window.  OpenUtau's bridge uses it too.
PORT_FIRST = 43102
PORT_LAST = 43111

#: serverInfo.name -> the engine key used elsewhere in this package.  A name
#: that is not listed is resolved from the bridge's own status call.
_BRIDGE_ENGINE = {
    "LhCeVioBridge": "cevio",
    "LhBridge": "native",  # native reports the real engine in its status call
    "openutau-mcp-bridge": "openutau",
    "OpenUtau.Plugin.McpBridge": "openutau",
}

#: Budget for probing a bridge - discovery and tool listing answer immediately.
_TIMEOUT = 1.5

#: Budget for calling a tool.  These tools drive a live editor on the far side of
#: the socket, and a real operation is not fast: loading a voice into CeVIO's song
#: engine takes about 27 seconds on first use.  1.5s reported every such call as a
#: timeout while the bridge went on to complete it, so calls get their own budget.
_CALL_TIMEOUT = 120.0


def _rpc(endpoint: str, method: str, params: dict[str, Any] | None = None, timeout: float = _TIMEOUT) -> dict[str, Any]:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", "replace")
    if not payload:
        return {}
    return json.loads(payload)


def _tool_text(reply: dict[str, Any]) -> Any:
    """Unwrap an MCP ``tools/call`` reply into the tool's own JSON payload."""
    result = reply.get("result") or {}
    content = result.get("content") or []
    if not content:
        return {"ok": False, "error": reply.get("error", {}).get("message", "empty reply")}
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": True, "text": text}


def endpoints(timeout: float = 0.4) -> list[dict[str, Any]]:
    """Every bridge answering in the port window, with its identity."""
    found: list[dict[str, Any]] = []
    for port in range(PORT_FIRST, PORT_LAST + 1):
        endpoint = f"http://127.0.0.1:{port}/mcp"
        try:
            reply = _rpc(endpoint, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}, timeout=timeout)
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError):
            continue
        info = (reply.get("result") or {}).get("serverInfo") or {}
        name = info.get("name", "")
        found.append(
            {
                "port": port,
                "endpoint": endpoint,
                "bridge": name,
                "bridge_version": info.get("version", ""),
                "protocol_version": (reply.get("result") or {}).get("protocolVersion", ""),
                "engine": _BRIDGE_ENGINE.get(name, "unknown"),
            }
        )
    return found


def discover(engine: str = "") -> list[dict[str, Any]]:
    """Bridges, optionally filtered to one engine.

    A native bridge reports a generic name (it does not know which editor it was
    injected into until it runs), so its real engine is read from its own status
    call rather than guessed from the port.
    """
    found = endpoints()
    resolved: list[dict[str, Any]] = []
    for entry in found:
        if entry["engine"] in ("native", "unknown"):
            payload = status(entry["endpoint"])
            detected = payload.get("app", {}).get("engine", "")
            if detected:
                entry = dict(entry, engine=detected)
        resolved.append(entry)
    if not engine:
        return resolved
    wanted = engine.strip().lower()
    return [entry for entry in resolved if entry["engine"] == wanted]


def for_engine(engine: str) -> dict[str, Any] | None:
    """The first endpoint for an engine, or None."""
    matches = discover(engine)
    return matches[0] if matches else None


def _status_tool(endpoint: str) -> str:
    """The bridge's own status tool name (``cevio_status``, ``voisona_status``).

    Derived from the published tool list rather than guessed, because a native
    bridge's prefix depends on which editor it landed in.
    """
    for tool in tools(endpoint):
        name = tool.get("name", "")
        if name.endswith("_status"):
            return name
    return ""


def status(endpoint: str) -> dict[str, Any]:
    """The bridge's own status payload (version, app baseline, capabilities)."""
    name = _status_tool(endpoint)
    if not name:
        return {}
    return call(endpoint, name, {})


def tools(endpoint: str) -> list[dict[str, Any]]:
    """The tool list a bridge publishes."""
    try:
        reply = _rpc(endpoint, "tools/list")
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return [{"error": f"{type(exc).__name__}: {exc}"}]
    return (reply.get("result") or {}).get("tools", [])


def call(
    endpoint: str,
    tool: str,
    arguments: dict[str, Any] | None = None,
    timeout: float = _CALL_TIMEOUT,
) -> dict[str, Any]:
    """Call one tool on a bridge and return its JSON payload."""
    try:
        reply = _rpc(endpoint, "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout)
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except (OSError, TimeoutError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"bridge sent invalid JSON: {exc}"}
    return _tool_text(reply)


def tool_prefix(endpoint: str) -> str:
    """The tool-name prefix a bridge uses (``cevio_``, ``voisona_``, ...)."""
    name = _status_tool(endpoint)
    return name[: -len("_status")] + "_" if name else ""
