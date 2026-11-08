"""DLL injection: get our agent inside a running editor.

This is the part that makes "direct control" more than menu clicking.  Once
:file:`lhai_agent.dll` is inside the process it can read the app's memory, follow
pointers to the song model, and answer on a named pipe - none of which is
possible from outside the process.

The classic four calls are used, in this order, for a reason:

``OpenProcess``      needs ``PROCESS_VM_*`` + ``CREATE_THREAD``; a same-user
                     process grants it, an elevated one does not.
``VirtualAllocEx``   room for the DLL path.
``WriteProcessMemory`` the path as UTF-16.
``CreateRemoteThread`` ``LoadLibraryW`` on that path, then wait on the thread
                     handle so a failure is reported as a failure.

Ejection is symmetric (``FreeLibrary`` + ``FreeLibraryAndExitThread`` inside the
agent), which matters because a half-unloaded agent holding a pipe handle would
keep the editor in a state where the next inject fails.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lhai.live import win32

__all__ = ["agent_dll_path", "inject", "is_loaded", "eject", "loaded_modules", "module_base_in", "unload_module"]

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_CREATE_THREAD = 0x0002
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

k32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

#: The agent ships beside the package: ``lhai/live/native/lhai_agent.dll``.
def agent_dll_path() -> Path:
    return Path(__file__).with_name("native") / "lhai_agent.dll"


@dataclass(slots=True)
class InjectResult:
    ok: bool
    pid: int
    dll: str
    hmodule: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, "pid": self.pid, "dll": self.dll, "channel": "agent"}
        if self.hmodule:
            payload["hmodule"] = hex(self.hmodule)
        if self.error:
            payload["error"] = self.error
        return payload


def _enable_debug_privilege() -> bool:
    """``SeDebugPrivilege`` - required for a process the user did not start."""
    if os.name != "nt":
        return False
    try:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0x0020, 0x0008
        SE_PRIVILEGE_ENABLED = 0x0002

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        token = wt.HANDLE()
        if not advapi.OpenProcessToken(
            k32.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
        ):
            return False
        luid = LUID()
        if not advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, SE_PRIVILEGE_ENABLED)))
        return bool(advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None))
    except Exception:  # noqa: BLE001
        return False


def inject(pid: int, dll: str | Path | None = None, wait: float = 8.0) -> InjectResult:
    """Load the agent DLL into ``pid``.  Returns the module handle on success."""
    target = Path(dll) if dll else agent_dll_path()
    if os.name != "nt":
        return InjectResult(False, pid, str(target), error="Windows only")
    if not target.exists():
        return InjectResult(False, pid, str(target), error=f"{target} not built (see agents/native/build.ps1)")
    if win32.is_64bit(os.getpid()) != win32.is_64bit(pid):
        return InjectResult(False, pid, str(target), error="bitness mismatch between agent and target")

    _enable_debug_privilege()
    access = PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    process = k32.OpenProcess(access, False, int(pid))
    if not process:
        return InjectResult(False, pid, str(target), error=f"OpenProcess failed ({ctypes.get_last_error()}); try running as administrator")

    hmodule = 0
    try:
        payload = str(target).encode("utf-16-le") + b"\x00\x00"
        size = len(payload)
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        remote = k32.VirtualAllocEx(process, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote:
            return InjectResult(False, pid, str(target), error=f"VirtualAllocEx failed ({ctypes.get_last_error()})")
        try:
            written = ctypes.c_size_t(0)
            if not k32.WriteProcessMemory(process, ctypes.c_void_p(remote), payload, size, ctypes.byref(written)):
                return InjectResult(False, pid, str(target), error=f"WriteProcessMemory failed ({ctypes.get_last_error()})")

            load_library = _remote_proc("LoadLibraryW")
            k32.CreateRemoteThread.restype = wt.HANDLE
            thread = k32.CreateRemoteThread(process, None, 0, ctypes.c_void_p(load_library), ctypes.c_void_p(remote), 0, None)
            if not thread:
                return InjectResult(False, pid, str(target), error=f"CreateRemoteThread failed ({ctypes.get_last_error()})")
            try:
                k32.WaitForSingleObject(thread, int(wait * 1000))
                exit_code = wt.DWORD(0)
                k32.GetExitCodeThread(thread, ctypes.byref(exit_code))
                hmodule = int(exit_code.value)
            finally:
                k32.CloseHandle(thread)
        finally:
            k32.VirtualFreeEx(process, ctypes.c_void_p(remote), 0, MEM_RELEASE)
    finally:
        k32.CloseHandle(process)

    if not hmodule:
        return InjectResult(False, pid, str(target), error="LoadLibraryW returned 0 - check the agent's dependencies (64-bit?)")
    return InjectResult(True, pid, str(target), hmodule=hmodule)


def _remote_proc(name: str) -> int:
    """Address of ``kernel32!name``, identical in every process of the same arch."""
    return int(ctypes.cast(getattr(k32, name), ctypes.c_void_p).value or 0)


_TH32CS_SNAPMODULE = 0x00000008
_TH32CS_SNAPMODULE32 = 0x00000010


class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("th32ModuleID", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("GlblcntUsage", wt.DWORD),
        ("ProccntUsage", wt.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wt.DWORD),
        ("hModule", wt.HMODULE),
        ("szModule", wt.WCHAR * 256),
        ("szExePath", wt.WCHAR * 260),
    ]


def loaded_modules(pid: int) -> list[dict[str, Any]]:
    snapshot = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32, int(pid))
    if snapshot == -1:
        return []
    out: list[dict[str, Any]] = []
    try:
        entry = _MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(_MODULEENTRY32W)
        if not k32.Module32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            out.append(
                {
                    "name": entry.szModule,
                    "path": entry.szExePath,
                    "base": int(ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0),
                    "size": int(entry.modBaseSize),
                }
            )
            if not k32.Module32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        k32.CloseHandle(snapshot)
    return out


def is_loaded(pid: int, name: str = "lhai_agent.dll") -> bool:
    return any(m["name"].lower() == name.lower() for m in loaded_modules(pid))


def module_base_in(pid: int, name: str) -> int:
    """Base address of a module already loaded in ``pid``, or 0."""
    for module in loaded_modules(pid):
        if module["name"].lower() == name.lower():
            return int(module["base"])
    return 0


def _export_rva(path: str | Path, export: str) -> int:
    """RVA of a named export, read from the on-disk PE.

    The loaded image has the same RVAs as the file it was mapped from, so this
    plus the remote module base gives the address to run with CreateRemoteThread.
    """
    import struct

    data = Path(path).read_bytes()
    if len(data) < 0x40 or data[:2] != b"MZ":
        return 0
    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        return 0
    coff = pe_offset + 4
    (size_of_optional,) = struct.unpack_from("<H", data, coff + 16)
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    directory = optional + (0x60 if magic == 0x10B else 0x70)
    (export_rva, export_size) = struct.unpack_from("<II", data, directory)
    if not export_rva:
        return 0

    def rva_to_offset(rva: int) -> int:
        (sections,) = struct.unpack_from("<H", data, coff + 2)
        section = optional + size_of_optional
        for _ in range(sections):
            name = data[section : section + 8].rstrip(b"\0")
            (virtual_size, virtual_address, raw_size, raw_pointer) = struct.unpack_from("<IIII", data, section + 8)
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                return raw_pointer + (rva - virtual_address)
            section += 40
        return 0

    exports_offset = rva_to_offset(export_rva)
    if not exports_offset:
        return 0
    (number_of_names,) = struct.unpack_from("<I", data, exports_offset + 24)
    (address_of_functions,) = struct.unpack_from("<I", data, exports_offset + 28)
    (address_of_names,) = struct.unpack_from("<I", data, exports_offset + 32)
    (address_of_ordinals,) = struct.unpack_from("<I", data, exports_offset + 36)
    names_offset = rva_to_offset(address_of_names)
    ordinals_offset = rva_to_offset(address_of_ordinals)
    functions_offset = rva_to_offset(address_of_functions)
    for index in range(number_of_names):
        (name_rva,) = struct.unpack_from("<I", data, names_offset + index * 4)
        name_offset = rva_to_offset(name_rva)
        if name_offset == 0:
            continue
        end = data.index(b"\0", name_offset)
        if data[name_offset:end].decode("ascii", "replace") != export:
            continue
        (ordinal,) = struct.unpack_from("<H", data, ordinals_offset + index * 2)
        (function_rva,) = struct.unpack_from("<I", data, functions_offset + ordinal * 4)
        return function_rva
    return 0


def unload_module(pid: int, name: str, image: str | Path | None = None, export: str = "", timeout: float = 8.0) -> dict[str, Any]:
    """Unload a module we injected by running its own unload export in-process.

    ``FreeLibrary`` from outside does not work here: a thread cannot free the
    module it is running in, so the export schedules the unload on a fresh thread
    inside the target and this waits for the module to leave the list.
    """
    base = module_base_in(pid, name)
    if not base:
        return {"ok": True, "pid": pid, "unloaded": False, "note": "not loaded"}
    if image is None:
        return {"ok": False, "pid": pid, "error": "image path is required to resolve the unload export"}
    rva = _export_rva(image, export)
    if not rva:
        return {"ok": False, "pid": pid, "error": f"export {export!r} not found in {image}"}

    _enable_debug_privilege()
    access = PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
    process = k32.OpenProcess(access, False, int(pid))
    if not process:
        return {"ok": False, "pid": pid, "error": f"OpenProcess failed ({ctypes.get_last_error()})"}
    try:
        k32.CreateRemoteThread.restype = wt.HANDLE
        thread = k32.CreateRemoteThread(process, None, 0, ctypes.c_void_p(base + rva), None, 0, None)
        if not thread:
            return {"ok": False, "pid": pid, "error": f"CreateRemoteThread failed ({ctypes.get_last_error()})"}
        try:
            k32.WaitForSingleObject(thread, int(timeout * 1000))
        finally:
            k32.CloseHandle(thread)
    finally:
        k32.CloseHandle(process)

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_loaded(pid, name):
            return {"ok": True, "pid": pid, "unloaded": True}
        time.sleep(0.2)
    return {"ok": False, "pid": pid, "error": f"{name} still loaded after {export}"}


def eject(pid: int, name: str = "lhai_agent.dll") -> dict[str, Any]:
    """Ask the agent to unload itself (``FreeLibraryAndExitThread``)."""
    from lhai.live import agent

    result = agent.call(pid, "UNLOAD", timeout=3.0)
    if not result.get("ok"):
        return {"ok": False, "pid": pid, "error": result.get("error", "agent did not answer")}
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not is_loaded(pid, name):
            return {"ok": True, "pid": pid, "unloaded": True}
        time.sleep(0.2)
    return {"ok": False, "pid": pid, "error": "still loaded after UNLOAD"}
