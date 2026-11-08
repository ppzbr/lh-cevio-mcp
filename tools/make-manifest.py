"""Write MANIFEST.txt: the sha256 of every committed file.

    python tools/make-manifest.py

Run it after any change to the tree, and before committing.  `tests/test_repo.py`
fails when the manifest disagrees with the working tree, which is what keeps a
hand-edited file from being shipped under a stale hash.

The skip list mirrors .gitignore: build output, bytecode, and the files a run
writes beside the bridge (boot logs, the status snapshot, the loader's target
hint) are not part of the repository and are not listed.
"""

from __future__ import annotations

import hashlib
import sys
from fnmatch import fnmatchcase
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MANIFEST = "MANIFEST.txt"

# `bin` and `obj` are .NET build output: they never appear in the Python
# repositories, and listing them here keeps this file identical in all of them.
SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "build",
    "bin",
    "obj",
    ".mypy_cache",
    ".ruff_cache",
}
# `pip install -e .` (Step 0 of prompt.md) writes an egg-info directory into the
# project root.  It is build output, not part of the repository, and it must not
# make the manifest look incomplete.
SKIP_ENDINGS = (".egg-info", ".dist-info")
SKIP_SUFFIXES = (".pyc", ".pyo", ".lib", ".exp", ".pdb")
SKIP_NAMES = {
    MANIFEST,
    # Diagnostics a run drops beside the binaries.  The list is the union for the
    # whole bridge family on purpose, so this file stays identical everywhere and
    # a name that never occurs here costs nothing.
    "LhCeVioBridge.boot.txt",
    "LhCeVioBridge.status.json",
    "LhCeVioLoader.boot.txt",
    "LhCeVioLoader.target.txt",
    "lhai_bridge.boot.txt",
    "lhai_bridge.status.json",
}

# Patterns this repository's own .gitignore excludes and the manifest must too: the
# hand-built helper executables (their sources are committed, the exes are not), and
# the copies a repackage parks while an editor holds a file open.  In fnmatch `*`
# crosses `/`, which is what makes these readable as one-liners.
SKIP_GLOBS = (
    "*/load_host_*.exe",
    "*/hotkey_host.exe",
    "*/vocalpatch.ini",
    "*.locked-*",
)

HEADER = (
    "# sha256 of every file in this repository\n"
    "# MANIFEST.txt itself is not listed.  Regenerate with:\n"
    "#   python tools/make-manifest.py\n"
    "# Verify by hand with:\n"
    "#   python tools/make-manifest.py --check\n"
)


def files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS or part.endswith(SKIP_ENDINGS) for part in rel.parts):
            continue
        if path.name.endswith(SKIP_SUFFIXES) or path.name in SKIP_NAMES:
            continue
        if any(fnmatchcase(rel.as_posix(), pattern) for pattern in SKIP_GLOBS):
            continue
        out.append(rel)
    return out


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str]) -> int:
    entries = {rel.as_posix(): digest(REPO / rel) for rel in files()}

    if "--check" in argv:
        listed = {}
        for line in (REPO / MANIFEST).read_text(encoding="utf-8").splitlines():
            parts = line.split(maxsplit=1)
            if len(parts) == 2 and len(parts[0]) == 64:
                listed[parts[1].strip()] = parts[0]
        bad = 0
        for name, wanted in sorted(entries.items()):
            got = listed.get(name)
            if got is None:
                print(f"missing from manifest: {name}")
                bad += 1
            elif got != wanted:
                print(f"hash mismatch:        {name}")
                bad += 1
        for name in sorted(set(listed) - set(entries)):
            print(f"listed but absent:    {name}")
            bad += 1
        print("ok" if not bad else f"{bad} problem(s)")
        return 1 if bad else 0

    lines = [HEADER.rstrip("\n")]
    lines += [f"{value}  {name}" for name, value in sorted(entries.items())]
    (REPO / MANIFEST).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {MANIFEST}: {len(entries)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
