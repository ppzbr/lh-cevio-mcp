// LhCeVioLoader.dll - inject the managed CeVIO bridge into a *running* CeVIO AI.
//
// The bridge itself (LhCeVioBridge.dll) is loaded two ways:
//
//   1. AppDomainManager start-up hook (see bridges/cevio/install.ps1).  Clean, but
//      the CLR will only resolve that assembly from the application base - which
//      for CeVIO means C:\Program Files\CeVIO\CeVIO AI, so the installer needs
//      administrator rights.
//
//   2. This loader.  A native DLL that, once inside the process, starts the CLR
//      and asks it to run LhCeVio.Bridge.Start from the bridge assembly at an
//      explicit path.  No administrator rights (injecting into your own,
//      non-elevated process does not need them), no files written into Program
//      Files, and no restart.
//
// Both end in the same place: LhCeVioBridge running in-process with its MCP
// endpoint up.  This one is preferred when the user is not an administrator, and
// it is the only one that can attach to an editor that is already open.
//
// How the CLR is started, and why this exact call:
//
//   mscoree!CorBindToRuntimeEx is the documented way to reach a CLR 4 host from
//   native code, and it is accepted with IID_ICLRRuntimeHost as the requested
//   interface.  ICLRRuntimeHost has a single method that does what is needed -
//   ExecuteInDefaultAppDomain - with a static-int(string) entry point, which is
//   exactly the shape Bridge.Start already has.  No IDispatch, no reflection
//   plumbing from this side.
//
//   The vtable is called by slot index rather than through a header, so this file
//   depends on nothing but kernel32 and whatever the process already has.
//
// Build: bridges/cevio/build.ps1   (zig c++, no MSVC)

#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <string>

// ---------------------------------------------------------------- tiny helpers
static std::string narrow(const std::wstring &s) {
    if (s.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0, nullptr, nullptr);
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n, nullptr, nullptr);
    return out;
}

static std::wstring widen(const std::string &s) {
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out((size_t)n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), &out[0], n);
    return out;
}

static std::string hex_hr(HRESULT hr) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "0x%08lX", (unsigned long)hr);
    return std::string(buf);
}

static HMODULE g_self = nullptr;
static std::wstring g_dir;

static void log_line(const std::string &line) {
    if (g_dir.empty()) return;
    std::string path = narrow(g_dir) + "\\LhCeVioLoader.boot.txt";
    HANDLE file = CreateFileA(path.c_str(), FILE_APPEND_DATA, FILE_SHARE_READ, nullptr, OPEN_ALWAYS, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) return;
    SYSTEMTIME st;
    GetSystemTime(&st);
    char stamp[64];
    std::snprintf(stamp, sizeof stamp, "%04u-%02u-%02uT%02u:%02u:%02uZ ",
                  st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond);
    std::string text = std::string(stamp) + line + "\r\n";
    DWORD wrote = 0;
    SetFilePointer(file, 0, nullptr, FILE_END);
    WriteFile(file, text.data(), (DWORD)text.size(), &wrote, nullptr);
    CloseHandle(file);
}

// ------------------------------------------------------------------ CLR vtable
// A minimal, self-contained view of ICLRRuntimeHost.
//
// The slot order matters and is not guessable.  From mscoree.idl it is:
//
//    0 QueryInterface            4 Stop               8 ExecuteInAppDomain
//    1 AddRef                    5 SetHostControl     9 GetCurrentAppDomainId
//    2 Release                   6 GetCLRControl     10 ExecuteApplication
//    3 Start                     7 UnloadAppDomain   11 ExecuteInDefaultAppDomain
//
// ExecuteInDefaultAppDomain is last, not eighth.  Slot 8 is ExecuteInAppDomain,
// which takes a numeric domain id where this code was passing an assembly path -
// so it answered "that AppDomain is not loaded" (COR_E_APPDOMAINUNLOADED) instead
// of ever running anything.
struct ClrHost {
    void **vtable;

    HRESULT start() {
        return ((HRESULT(STDMETHODCALLTYPE *)(void *))vtable[3])(this);
    }
    HRESULT execute_in_default_app_domain(
        LPCWSTR assembly, LPCWSTR type, LPCWSTR method, LPCWSTR argument, DWORD *result) {
        return ((HRESULT(STDMETHODCALLTYPE *)(void *, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, DWORD *))vtable[11])(
            this, assembly, type, method, argument, result);
    }
    void release() {
        ((ULONG(STDMETHODCALLTYPE *)(void *))vtable[2])(this);
    }
};

struct Guid {
    uint32_t d1;
    uint16_t d2;
    uint16_t d3;
    uint8_t d4[8];
};

// {90F1A06E-7712-4762-86B5-7A5EBA6BDB02}
// Note the E: this is CLSID_CLRRuntimeHost, the coclass that actually implements
// ICLRRuntimeHost.  CLSID_CorRuntimeHost (CB2F6723-...) looks similar but only
// answers for ICorRuntimeHost, and asking it for ICLRRuntimeHost fails with
// E_NOINTERFACE - which is exactly what happened the first time this ran.
static const Guid kClsidClrRuntimeHost = {0x90F1A06E, 0x7712, 0x4762, {0x86, 0xB5, 0x7A, 0x5E, 0xBA, 0x6B, 0xDB, 0x02}};
// {90F1A06C-7712-4762-86B5-7A5EBA6BDB02}
static const Guid kIidClrRuntimeHost = {0x90F1A06C, 0x7712, 0x4762, {0x86, 0xB5, 0x7A, 0x5E, 0xBA, 0x6B, 0xDB, 0x02}};
// {CB2F6723-AB3A-11D2-9C40-00C04FA30A3E}
static const Guid kClsidCorRuntimeHost = {0xCB2F6723, 0xAB3A, 0x11D2, {0x9C, 0x40, 0x00, 0xC0, 0x4F, 0xA3, 0x0A, 0x3E}};

typedef HRESULT(STDMETHODCALLTYPE *CorBindToRuntimeExFn)(
    LPCWSTR version, LPCWSTR build_flavor, DWORD startup_flags,
    const Guid *clsid, const Guid *iid, void **out);

// ----------------------------------------------- the documented CLR entry point
// CLRCreateInstance is the supported way in from CLR 4 onwards, and it is the one
// that behaves when the process already has a runtime up - which is always true
// here, since CeVIO is itself managed.  CorBindToRuntimeEx is the older, weaker
// door; it is kept only as a fallback.
//
// {9280188D-0E8E-4867-B30C-7FA83884E8DE}
static const Guid kClsidClrMetaHost = {0x9280188D, 0x0E8E, 0x4867, {0xB3, 0x0C, 0x7F, 0xA8, 0x38, 0x84, 0xE8, 0xDE}};
// {D332DB9E-B9B3-4125-8207-A14884F53216}
static const Guid kIidClrMetaHost = {0xD332DB9E, 0xB9B3, 0x4125, {0x82, 0x07, 0xA1, 0x48, 0x84, 0xF5, 0x32, 0x16}};

// Minimal view of ICLRMetaHost.  Only GetRuntime (slot 3) is used; the rest of the
// interface is left as unused slots.
struct ClrMetaHost {
    void **vtable;

    HRESULT get_runtime(LPCWSTR version, const Guid *iid, void **out) {
        return ((HRESULT(STDMETHODCALLTYPE *)(void *, LPCWSTR, const Guid *, void **))vtable[3])(
            this, version, iid, out);
    }
    void release() {
        ((ULONG(STDMETHODCALLTYPE *)(void *))vtable[2])(this);
    }
};

typedef HRESULT(STDMETHODCALLTYPE *CLRCreateInstanceFn)(const Guid *clsid, const Guid *iid, void **out);

// {BD39D1D2-BA2F-486A-89B0-B4B0CB466891}
static const Guid kIidClrRuntimeInfo = {0xBD39D1D2, 0xBA2F, 0x486A, {0x89, 0xB0, 0xB4, 0xB0, 0xCB, 0x46, 0x68, 0x91}};

// Minimal view of ICLRRuntimeInfo.  GetRuntime hands back *this* interface, not the
// runtime host, and GetInterface is the second half of that handshake.
//
// The full order is: QI, AddRef, Release, GetVersionString, GetRuntimeDirectory,
// IsLoaded, LoadErrorString, LoadLibrary, GetProcAddress, GetInterface, ...  Slot 6
// (LoadErrorString) is easy to miss, and dropping it shifts every later slot by one
// - which lands GetInterface on GetProcAddress and faults the process.
struct ClrRuntimeInfo {
    void **vtable;

    HRESULT get_interface(const Guid *clsid, const Guid *iid, void **out) {
        return ((HRESULT(STDMETHODCALLTYPE *)(void *, const Guid *, const Guid *, void **))vtable[9])(
            this, clsid, iid, out);
    }
    void release() {
        ((ULONG(STDMETHODCALLTYPE *)(void *))vtable[2])(this);
    }
};

// ---------------------------------------------------------------- target file
// Which managed assembly to run.  A file beside the loader wins, so the loader
// stays path-agnostic; without one, the bridge is expected next to the loader.
static std::wstring bridge_path() {
    wchar_t buffer[MAX_PATH] = {0};
    std::string override_file = narrow(g_dir) + "\\LhCeVioLoader.target.txt";
    HANDLE file = CreateFileA(override_file.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (file != INVALID_HANDLE_VALUE) {
        char text[MAX_PATH * 2] = {0};
        DWORD got = 0;
        ReadFile(file, text, sizeof text - 1, &got, nullptr);
        CloseHandle(file);
        std::string trimmed(text, (size_t)got);
        while (!trimmed.empty() && (trimmed.back() == '\r' || trimmed.back() == '\n' || trimmed.back() == ' ')) trimmed.pop_back();
        size_t start = 0;
        while (start < trimmed.size() && (trimmed[start] == ' ' || trimmed[start] == '\t')) ++start;
        if (!trimmed.empty()) return widen(trimmed.substr(start));
    }
    if (GetModuleFileNameW(g_self, buffer, MAX_PATH)) {
        std::wstring self = buffer;
        size_t slash = self.find_last_of(L"\\/");
        if (slash != std::wstring::npos) return self.substr(0, slash + 1) + L"LhCeVioBridge.dll";
    }
    return L"LhCeVioBridge.dll";
}

// ------------------------------------------------------------- eject a module
// While the loader is being iterated on it can end up mapped more than once: the
// file it was loaded from stays locked for as long as any copy is resident, so a
// fix cannot be built over it.  This runs *inside* the target process, where
// FreeLibrary on any resident module is legal, and is how a stale copy is let go
// without restarting the editor.
//
// The request is a file, LhCeVioLoader.eject.txt, holding one module base in hex.
// After freeing it the file is removed, so the loader falls through to a normal
// boot on the next injection.
static bool handle_eject() {
    std::string path = narrow(g_dir) + "\\LhCeVioLoader.eject.txt";
    HANDLE file = CreateFileA(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING, 0, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;

    char text[64] = {0};
    DWORD got = 0;
    ReadFile(file, text, sizeof text - 1, &got, nullptr);
    CloseHandle(file);
    DeleteFileA(path.c_str());

    unsigned long long base = 0;
    if (std::sscanf(text, "%llx", &base) != 1 || base == 0) {
        log_line("eject request was unreadable; ignoring it");
        return false;
    }
    std::string hex = hex_hr((HRESULT)(ULONG_PTR)base);
    BOOL freed = FreeLibrary((HMODULE)(ULONG_PTR)base);
    log_line("eject: FreeLibrary(" + hex + ") -> " + (freed ? "ok" : "failed"));
    return true;
}

// -------------------------------------------------------------------- the boot
static void boot() {
    if (handle_eject()) return;

    std::wstring assembly = bridge_path();
    log_line("booting pid=" + std::to_string(GetCurrentProcessId()) +
             " bridge=" + narrow(assembly));

    if (GetFileAttributesW(assembly.c_str()) == INVALID_FILE_ATTRIBUTES) {
        log_line("bridge assembly not found: " + narrow(assembly));
        return;
    }

    HMODULE mscoree = LoadLibraryW(L"mscoree.dll");
    if (!mscoree) {
        log_line("mscoree.dll is not loaded in this process - not a .NET Framework app?");
        return;
    }
    ClrHost *host = nullptr;

    // Preferred route: the metahost API.  It is the documented one, and it is the
    // one that reflects a runtime the host application already started rather than
    // a fresh legacy binding - which matters here, because CeVIO's runtime is up
    // before this code ever runs.
    auto create_instance = (CLRCreateInstanceFn)GetProcAddress(mscoree, "CLRCreateInstance");
    if (create_instance) {
        ClrMetaHost *meta = nullptr;
        HRESULT mhr = create_instance(&kClsidClrMetaHost, &kIidClrMetaHost, (void **)&meta);
        log_line("CLRCreateInstance(CLRMetaHost) -> " + hex_hr(mhr));
        if (SUCCEEDED(mhr) && meta) {
            // Two steps by design: GetRuntime yields an ICLRRuntimeInfo for the
            // runtime, and the host interface is obtained from that.  Asking for
            // IID_ICLRRuntimeHost directly here fails with E_NOINTERFACE.
            ClrRuntimeInfo *info = nullptr;
            HRESULT ihr = meta->get_runtime(L"v4.0.30319", &kIidClrRuntimeInfo, (void **)&info);
            log_line("ICLRMetaHost::GetRuntime(v4.0.30319) -> " + hex_hr(ihr));
            if (SUCCEEDED(ihr) && info) {
                HRESULT ghr = info->get_interface(&kClsidClrRuntimeHost, &kIidClrRuntimeHost, (void **)&host);
                log_line("ICLRRuntimeInfo::GetInterface(ICLRRuntimeHost) -> " + hex_hr(ghr));
                if (FAILED(ghr)) host = nullptr;
                info->release();
            }
            meta->release();
        }
    }

    // Fallback: the older binding.  Only used if the metahost route is unavailable.
    if (!host) {
        auto bind = (CorBindToRuntimeExFn)GetProcAddress(mscoree, "CorBindToRuntimeEx");
        if (!bind) {
            log_line("neither CLRCreateInstance nor CorBindToRuntimeEx is available");
            return;
        }
        // The coclass must be CLSID_CLRRuntimeHost, not the lookalike
        // CLSID_CorRuntimeHost; only the former answers IID_ICLRRuntimeHost.
        HRESULT hr = bind(L"v4.0.30319", L"wks", 0x00000001, &kClsidClrRuntimeHost, &kIidClrRuntimeHost, (void **)&host);
        log_line("CorBindToRuntimeEx(CLRRuntimeHost) -> " + hex_hr(hr));
        if (FAILED(hr) || !host) {
            log_line("could not reach the CLR host; no bridge loaded");
            return;
        }
    }

    HRESULT hr = host->start();
    // If the runtime is already running - which CeVIO's is - this reports S_FALSE
    // rather than failing.  Both outcomes are fine here.
    log_line("ICLRRuntimeHost::Start -> " + hex_hr(hr));

    DWORD result = 0;
    hr = host->execute_in_default_app_domain(
        assembly.c_str(), L"LhCeVio.Bridge", L"Start", L"injected", &result);
    log_line("ExecuteInDefaultAppDomain -> " + hex_hr(hr) + " result=" + std::to_string(result));

    if (FAILED(hr)) {
        log_line("the bridge did not start; check LhCeVioBridge.boot.txt beside it for a later stage");
    }
    host->release();
}

// ------------------------------------------------------------------ entry point
static DWORD WINAPI boot_thread(LPVOID) {
    // No SEH here: clang targets without MSVC's __try/__except do not have it, and
    // boot() only calls documented Win32 entry points plus one COM vtable slot -
    // there is no raw pointer arithmetic to guard.
    boot();
    return 0;
}

BOOL APIENTRY DllMain(HMODULE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_self = module;
        DisableThreadLibraryCalls(module);
        wchar_t self[MAX_PATH] = {0};
        if (GetModuleFileNameW(module, self, MAX_PATH)) {
            std::wstring path = self;
            size_t slash = path.find_last_of(L"\\/");
            g_dir = (slash == std::wstring::npos) ? L"." : path.substr(0, slash);
        }
        // Work off a fresh thread: DllMain holds the loader lock, and starting a
        // runtime from inside it is a classic way to deadlock the target.
        HANDLE thread = CreateThread(nullptr, 0, boot_thread, nullptr, 0, nullptr);
        if (thread) CloseHandle(thread);
    }
    return TRUE;
}

extern "C" __declspec(dllexport) unsigned long LhCeVioLoaderPid(void) {
    return GetCurrentProcessId();
}

// Let the host release this loader.  Called through a remote thread whose start
// address is inside this module, so FreeLibraryAndExitThread is the correct and
// safe way out; the managed bridge keeps running, because the CLR domain still
// holds a reference to the assembly the loader started.
extern "C" __declspec(dllexport) void LhCeVioLoaderUnload(void) {
    HMODULE self = g_self;
    if (self) FreeLibraryAndExitThread(self, 0);
}
