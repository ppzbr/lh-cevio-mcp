"""Live control of the four singing editors - the layer that touches a *running* app.

The file-based adapters in :mod:`lhai.mcp.engines` can write a project but cannot
move a slider in an editor that is already open.  This package is the other half:
it finds the process, talks to it, and reports exactly which channel it used.

Four channels, in order of preference, and each engine gets the best one it
actually supports::

    channel      needs                    works for
    -----------  -----------------------  -------------------------------------
    com          a registered ProgID      CeVIO AI (Talk remote service)
    lua          a script API             Synthesizer V Studio
    agent        an injected DLL          any 64-bit process (native or .NET)
    uia          UI Automation            every Win32/WPF/Qt window
    keys         focused window           every app (sendinput / hotkeys)

``dll`` is not the first resort: it is intrusive and version-fragile, so it is
used where the app exposes no scripting surface (VOCALOID 6, VoiSona song
editing) and the caller asked for model-level access rather than menu clicks.

Nothing here guesses.  Every entry point returns ``{"ok": bool, ...}`` and names
the channel it used, so a model driving this never believes it edited a score
through a path that silently did nothing.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
