"""UI Automation: see and press the widgets of a running editor.

Why UIA and not screenshots: coordinates break the moment the user moves a
window or changes their zoom, while an automation element survives all of that
and carries the control's *name* (`"Play"`, `"File"`, `"Save As"`) which is what
the caller actually means.

``comtypes`` generates the ``UIAutomationClient`` wrappers on first use, so there
is no hand-written COM boilerplate and no vendored typelib.  If ``comtypes`` is
missing the module still imports and every call returns an empty result with
``ok=False`` - the MCP layer reports that instead of crashing a tool call.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from typing import Any, Iterator

from lhai.live import win32

__all__ = [
    "available",
    "element_from_hwnd",
    "dump_tree",
    "find",
    "find_all",
    "invoke",
    "click",
    "set_text",
    "get_text",
    "toggle",
    "select",
    "describe",
    "element_at",
]

_KIND = "control type"


def _load() -> Any:
    """Import the generated wrappers once; ``None`` when comtypes is absent."""
    global _UIA, _CREATE, _AUTOMATION
    try:
        import comtypes.client

        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        _UIA = UIA
        _CREATE = comtypes.client.CreateObject
        _AUTOMATION = _CREATE(UIA.CUIAutomation, interface=UIA.IUIAutomation)
        return UIA
    except Exception:  # noqa: BLE001 - missing comtypes, or a broken typelib
        _UIA = None
        _AUTOMATION = None
        return None


_UIA: Any = None
_CREATE: Any = None
_AUTOMATION: Any = None
_LOADED = False


def _uia() -> Any:
    global _LOADED
    if not _LOADED:
        win32.dpi_aware()
        _load()
        _LOADED = True
    return _AUTOMATION


def available() -> tuple[bool, str]:
    if _uia() is None:
        return False, "comtypes / UIAutomationClient unavailable (pip install comtypes)"
    return True, ""


# --------------------------------------------------------------------------- #
# element helpers
# --------------------------------------------------------------------------- #
def element_from_hwnd(hwnd: int) -> Any:
    auto = _uia()
    if auto is None or not hwnd:
        return None
    try:
        return auto.ElementFromHandle(int(hwnd))
    except Exception:  # noqa: BLE001 - window died between calls
        return None


def _control_type_name(value: int) -> str:
    UIA = _UIA
    if UIA is None:
        return str(value)
    for name in dir(UIA):
        if name.startswith("UIA_") and name.endswith("ControlTypeId"):
            if getattr(UIA, name) == value:
                return name[len("UIA_") : -len("ControlTypeId")].lower()
    return str(value)


@dataclass(slots=True)
class Element:
    """A snapshot of one automation element, safe to return to a caller."""

    name: str = ""
    role: str = ""
    automation_id: str = ""
    class_name: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    enabled: bool = True
    offscreen: bool = False
    value: str = ""
    depth: int = 0
    children: list["Element"] = field(default_factory=list)

    def as_dict(self, with_children: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "role": self.role,
            "automation_id": self.automation_id,
            "class": self.class_name,
            "rect": list(self.rect),
            "enabled": self.enabled,
        }
        if self.offscreen:
            payload["offscreen"] = True
        if self.value:
            payload["value"] = self.value
        if with_children and self.children:
            payload["children"] = [c.as_dict(with_children) for c in self.children]
        return payload


def describe(el: Any, depth: int = 0) -> Element:
    """Read one element's current properties into plain data (never raises)."""
    info = Element(depth=depth)
    if el is None:
        return info

    def get(name: str, default: Any = "") -> Any:
        try:
            value = getattr(el, name)
        except Exception:  # noqa: BLE001 - element invalidated or property absent
            return default
        return value if value is not None else default

    info.name = str(get("CurrentName"))
    info.role = _control_type_name(int(get("CurrentControlType", 0)))
    info.automation_id = str(get("CurrentAutomationId"))
    info.class_name = str(get("CurrentClassName"))
    rect = get("CurrentBoundingRectangle", None)
    if rect is not None:
        try:
            info.rect = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        except Exception:  # noqa: BLE001
            pass
    info.enabled = bool(get("CurrentIsEnabled", True))
    info.offscreen = bool(get("CurrentIsOffscreen", False))
    value = get("CurrentValue", None)
    if isinstance(value, str):
        info.value = value
    return info


def _walker(raw: bool = False) -> Any:
    auto = _uia()
    if auto is None:
        return None
    try:
        return auto.RawViewWalker if raw else auto.ControlViewWalker
    except Exception:  # noqa: BLE001
        return None


def _children_of(el: Any, raw: bool = False, limit: int = 400) -> Iterator[Any]:
    walker = _walker(raw)
    if walker is None:
        return iter(())
    out: list[Any] = []
    try:
        child = walker.GetFirstChildElement(el)
        while child is not None and len(out) < limit:
            out.append(child)
            child = walker.GetNextSiblingElement(child)
    except Exception:  # noqa: BLE001 - tree changed mid-walk
        pass
    return iter(out)


def dump_tree(
    hwnd: int, depth: int = 6, raw: bool = False, max_nodes: int = 600
) -> Element | None:
    """Walk the control tree under ``hwnd`` (control view by default: less noise)."""
    root = element_from_hwnd(hwnd)
    if root is None:
        return None
    budget = max_nodes

    def walk(el: Any, level: int) -> Element:
        nonlocal budget
        info = describe(el, level)
        budget -= 1
        if level >= depth or budget <= 0:
            return info
        for child in _children_of(el, raw=raw):
            if budget <= 0:
                break
            info.children.append(walk(child, level + 1))
        return info

    return walk(root, 0)


# --------------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------------- #
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def find_all(
    hwnd: int,
    name: str = "",
    role: str = "",
    automation_id: str = "",
    class_name: str = "",
    limit: int = 20,
    max_nodes: int = 4000,
) -> list[Any]:
    """Live automation elements matching the given properties (``name`` is a substring).

    A depth-first walk of the control view is used instead of ``FindFirst``: the
    name filter is a substring, and composing a substring condition in UIA is
    more code than walking the tree, with no accuracy to gain.
    """
    root = element_from_hwnd(hwnd)
    if root is None:
        return []
    needle = name.lower()
    wanted_role = role.lower()
    found: list[Any] = []
    budget = max_nodes
    stack = [root]
    while stack and budget > 0 and len(found) < limit:
        el = stack.pop()
        budget -= 1
        info = describe(el)
        if needle and needle not in info.name.lower():
            pass
        elif wanted_role and info.role.lower() != wanted_role:
            pass
        elif automation_id and info.automation_id != automation_id:
            pass
        elif class_name and info.class_name != class_name:
            pass
        else:
            found.append(el)
        if budget > 0:
            children = list(_children_of(el, limit=400))
            stack.extend(reversed(children))
    return found


def find(
    hwnd: int,
    name: str = "",
    role: str = "",
    automation_id: str = "",
    class_name: str = "",
    timeout: float = 3.0,
) -> Any:
    """First matching element, re-trying until ``timeout`` (dialogs animate in)."""
    import time

    deadline = time.time() + max(0.0, timeout)
    while True:
        hits = find_all(hwnd, name=name, role=role, automation_id=automation_id, class_name=class_name, limit=1)
        if hits:
            return hits[0]
        if time.time() >= deadline:
            return None
        time.sleep(0.15)


def element_at(hwnd: int, x: int, y: int) -> Any:
    auto = _uia()
    if auto is None:
        return None
    try:
        return auto.ElementFromPoint(_POINT(int(x), int(y)))
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def _pattern(el: Any, pid_name: str, iface_name: str) -> Any:
    UIA = _UIA
    if el is None or UIA is None:
        return None
    try:
        raw = el.GetCurrentPattern(getattr(UIA, pid_name))
        if raw is None:
            return None
        return raw.QueryInterface(getattr(UIA, iface_name))
    except Exception:  # noqa: BLE001 - pattern unsupported
        return None


def invoke(el: Any) -> dict[str, Any]:
    """Invoke (press) an element: Invoke -> Legacy default action -> mouse click."""
    pattern = _pattern(el, "UIA_InvokePatternId", "IUIAutomationInvokePattern")
    if pattern is not None:
        try:
            pattern.Invoke()
            return {"ok": True, "via": "invoke"}
        except Exception as exc:  # noqa: BLE001
            last = f"invoke failed: {exc}"
    else:
        last = "no invoke pattern"
    legacy = _pattern(el, "UIA_LegacyIAccessiblePatternId", "IUIAutomationLegacyIAccessiblePattern")
    if legacy is not None:
        try:
            legacy.DoDefaultAction()
            return {"ok": True, "via": "legacy"}
        except Exception as exc:  # noqa: BLE001
            last = f"legacy failed: {exc}"
    clicked = click(el)
    if clicked.get("ok"):
        return {"ok": True, "via": "mouse"}
    return {"ok": False, "error": last}


def click(el: Any, button: str = "left") -> dict[str, Any]:
    """Click the element's centre with the real mouse."""
    info = describe(el)
    left, top, right, bottom = info.rect
    if right <= left or bottom <= top:
        return {"ok": False, "error": "element has no clickable rectangle (offscreen?)"}
    x, y = (left + right) // 2, (top + bottom) // 2
    # Nudge onto a real pixel inside the element, then click.
    win32.click_at(x, y, button=button)
    return {"ok": True, "via": "mouse", "at": [x, y], "name": info.name}


def set_text(el: Any, text: str) -> dict[str, Any]:
    """Set a text field's value through the Value pattern (no keyboard focus needed)."""
    pattern = _pattern(el, "UIA_ValuePatternId", "IUIAutomationValuePattern")
    if pattern is None:
        return {"ok": False, "error": "element has no Value pattern (try type_text after focus)"}
    try:
        pattern.SetValue(str(text))
        return {"ok": True, "via": "value"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def get_text(el: Any) -> str:
    pattern = _pattern(el, "UIA_ValuePatternId", "IUIAutomationValuePattern")
    if pattern is not None:
        try:
            return str(pattern.CurrentValue)
        except Exception:  # noqa: BLE001
            pass
    return describe(el).name


def toggle(el: Any, on: bool | None = None) -> dict[str, Any]:
    pattern = _pattern(el, "UIA_TogglePatternId", "IUIAutomationTogglePattern")
    if pattern is None:
        return {"ok": False, "error": "element has no Toggle pattern"}
    try:
        state = int(pattern.CurrentToggleState)
        want = 2 if (on is None and state == 0) or (on is True and state != 2) else 0 if on is False else None
        if want is not None and want != state:
            pattern.Toggle()
        return {"ok": True, "state": int(pattern.CurrentToggleState)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def state_of(el: Any) -> dict[str, Any]:
    """Read an element's *state* without changing it.

    A WPF radio button is a ``Toggle``; a list item is a ``SelectionItem``.  Both
    are the same question - "is this mode the active one?" - so they are answered
    through one call, which is what lets a caller read a toolbar back instead of
    assuming a click landed.
    """
    for pid, iface, attr in (
        ("UIA_TogglePatternId", "IUIAutomationTogglePattern", "CurrentToggleState"),
        ("UIA_SelectionItemPatternId", "IUIAutomationSelectionItemPattern", "CurrentIsSelected"),
    ):
        pattern = _pattern(el, pid, iface)
        if pattern is not None:
            try:
                value = getattr(pattern, attr)
            except Exception:  # noqa: BLE001
                continue
            if attr == "CurrentToggleState":
                return {"ok": True, "on": int(value) != 0, "state": int(value), "kind": "toggle"}
            return {"ok": True, "on": bool(value), "state": 1 if value else 0, "kind": "selection"}
    return {"ok": False, "error": "element has neither Toggle nor SelectionItem pattern"}


def select(el: Any) -> dict[str, Any]:
    for pid, iface, call in (
        ("UIA_SelectionItemPatternId", "IUIAutomationSelectionItemPattern", "Select"),
        ("UIA_ExpandCollapsePatternId", "IUIAutomationExpandCollapsePattern", "Expand"),
    ):
        pattern = _pattern(el, pid, iface)
        if pattern is not None:
            try:
                getattr(pattern, call)()
                return {"ok": True, "via": call.lower()}
            except Exception:  # noqa: BLE001
                continue
    return invoke(el)
