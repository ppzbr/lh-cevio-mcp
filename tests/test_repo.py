"""This repository must be self-describing and internally consistent.

Three kinds of failure this file exists to catch, all of which have happened in
the project this bridge came from:

* a shipped DLL that does not match the build record beside it, so a user
  installs a binary nobody built - the manifest and the build record are the two
  statements of provenance, and they must agree;
* a manifest that has drifted from the working tree, so a hand-edited file ships
  under a stale hash;
* a test run that exercises an `lhai` installed somewhere else, which reports
  green for code this repository does not contain.

It also asserts that the paths `prompt.md` tells an agent to run are present, and
that the ones its own steps produce were not committed as if they were source: a
prompt that names a file which is not here is worse than no prompt.
"""

from __future__ import annotations

import hashlib
import re
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "MANIFEST.txt"

# --------------------------------------------------------------------------- #
# what this repository ships, and how to prove it
# --------------------------------------------------------------------------- #
# CeVIO AI is the one editor in this family whose bridge is *built and shipped*,
# because the engine authenticates against CeVIO's own licensing inside the
# process and the bridge has to be the exact binary that was tested.  Everything
# the binaries claim about themselves is in one file beside them.
BUILD_INFO = "bridges/cevio/LhCeVioBridge.build-info.txt"

# (binary, the build record that documents it, the key holding its sha256)
PROVENANCE: list[tuple[str, str, str]] = [
    ("bridges/cevio/LhCeVioBridge.dll", BUILD_INFO, "Sha256"),
    ("bridges/cevio/native/LhCeVioLoader.dll", BUILD_INFO, "LoaderSha256"),
]

# (build record, key, expected value) - properties of the build that must hold.
# `TargetFramework` is the one that matters most: CeVIO AI is a .NET Framework
# 4.8 process, and a loader or bridge built for anything else loads, fails to
# resolve, and gives no clue why.  install.ps1 checks this before copying; this
# checks the file that ships.
FACTS: list[tuple[str, str, str]] = [
    (BUILD_INFO, "TargetFramework", "net48"),
    (BUILD_INFO, "Platform", "x64"),
    (BUILD_INFO, "Loader", "LhCeVioLoader.dll"),
]

# The files prompt.md tells the agent to run.  The regex test below covers every
# path in the prompt; this list is the ones that must never go away.
PROMPT_PATHS = [
    "bridges/cevio/build.ps1",
    "bridges/cevio/inject.py",
    "bridges/cevio/install.ps1",
    "bridges/cevio/uninstall.ps1",
    "bridges/cevio/LhCeVioBridge.dll",
    "bridges/cevio/native/LhCeVioLoader.dll",
    "lhai/live/inject.py",
    "lhai/mcp/server.py",
]

# Paths prompt.md names that its own steps produce.  Unlike the other
# repositories in the family this list is not only build output: the bridge
# writes a boot log and a status snapshot beside itself, and the loader writes a
# target hint, so a fresh clone legitimately does not have them.  They must never
# be committed either.
GENERATED = [
    "bridges/cevio/LhCeVioBridge.boot.txt",
    "bridges/cevio/LhCeVioBridge.status.json",
    "bridges/cevio/native/LhCeVioLoader.boot.txt",
]

# Every repository path in prompt.md, in either separator style and wherever it
# appears - prose or code block.  Requiring an extension keeps bare mentions of the
# directories (`lhai/`, `docs/`) and non-paths (`tools/list`) out of the check, and
# the lookbehind keeps a path inside another one out of it.
PROMPT_PATH_RE = re.compile(
    r"(?<![\w\\/.-])(?:bridges|lhai|tests|docs|tools|VocalPatch|xstudio)[\\/][\w./\\-]*\.\w+"
)

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
# `pip install -e .` writes these into the project root; they are build output.
SKIP_ENDINGS = (".egg-info", ".dist-info")
SKIP_SUFFIXES = (".pyc", ".pyo", ".lib", ".exp", ".pdb")
# Diagnostics a run drops beside the binaries.  Names this repository never
# produces are harmless here; the list is shared across the family on purpose.
SKIP_NAMES = {
    "MANIFEST.txt",
    "LhCeVioBridge.boot.txt",
    "LhCeVioBridge.status.json",
    "LhCeVioLoader.boot.txt",
    "LhCeVioLoader.target.txt",
    "lhai_bridge.boot.txt",
    "lhai_bridge.status.json",
}
# Patterns the rest of the family excludes by way of .gitignore and the manifest must
# too: the hand-built helper executables (their sources are committed, the exes are
# not), and the copies a repackage parks while an editor holds a file open.  None of
# these can occur in this repository; the list is the union for the whole family on
# purpose, so this file stays identical everywhere.  In fnmatch `*` crosses `/`.
SKIP_GLOBS = (
    "*/load_host_*.exe",
    "*/hotkey_host.exe",
    "*/vocalpatch.ini",
    "*.locked-*",
)


# --------------------------------------------------------------------------- #
# reading the tree
# --------------------------------------------------------------------------- #
def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tracked() -> set[str]:
    out = set()
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(REPO)
        if any(part in SKIP_DIRS or part.endswith(SKIP_ENDINGS) for part in rel.parts):
            continue
        if path.name.endswith(SKIP_SUFFIXES) or path.name in SKIP_NAMES:
            continue
        if any(fnmatchcase(rel.as_posix(), pattern) for pattern in SKIP_GLOBS):
            continue
        out.add(rel.as_posix())
    return out


def _manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([0-9a-f]{64})\s+(\S+)$", line)
        if match:
            entries[match.group(2)] = match.group(1)
    return entries


def _record(relative: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / relative).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------- #
# the manifest is the record of what is here
# --------------------------------------------------------------------------- #
def test_the_manifest_lists_every_file_and_only_files_that_exist() -> None:
    listed = set(_manifest())
    present = _tracked()
    assert present - listed == set(), "in the repository but not in MANIFEST.txt"
    assert listed - present == set(), "listed in MANIFEST.txt but not in the repository"


def test_every_manifest_hash_matches_its_file() -> None:
    wrong = [name for name, want in _manifest().items() if _hash(REPO / name) != want]
    assert wrong == [], "MANIFEST.txt disagrees with the files - run tools/make-manifest.py"


def test_the_manifest_does_not_list_itself() -> None:
    assert "MANIFEST.txt" not in _manifest()


# --------------------------------------------------------------------------- #
# provenance: the shipped binaries say what they are
# --------------------------------------------------------------------------- #
def test_every_shipped_binary_is_the_one_its_build_record_describes() -> None:
    if not PROVENANCE:
        pytest.skip("this repository ships no prebuilt binaries")
    for binary, record, key in PROVENANCE:
        want = _record(record)[key].upper()
        got = _hash(REPO / binary).upper()
        assert got == want, f"{binary} is not the binary {record} recorded"


def test_every_recorded_build_property_still_holds() -> None:
    if not FACTS:
        pytest.skip("this repository records no build properties")
    for record, key, want in FACTS:
        assert _record(record).get(key) == want, f"{record}: {key} is not {want}"


# --------------------------------------------------------------------------- #
# the tree is the one being tested
# --------------------------------------------------------------------------- #
def test_the_code_under_test_is_this_repository() -> None:
    """A copy imported from somewhere else would make every other test a statement
    about a different tree.  Repositories that ship no Python package skip."""
    package = REPO / "lhai" / "__init__.py"
    if not package.is_file():
        pytest.skip("this repository ships no Python package")
    import lhai

    assert Path(lhai.__file__).resolve() == package.resolve()


# --------------------------------------------------------------------------- #
# the prompt describes files that exist
# --------------------------------------------------------------------------- #
def test_the_files_the_prompt_names_are_present() -> None:
    if not PROMPT_PATHS:
        pytest.skip("this repository has no file checklist")
    missing = [name for name in PROMPT_PATHS if not (REPO / name).is_file()]
    assert missing == [], "prompt.md names files that are not here"


def test_every_path_prompt_md_mentions_exists() -> None:
    """The prompt is pasted into an agent that has never seen this repository, so
    every path it names must either be here, or be something its own steps build."""
    text = (REPO / "prompt.md").read_text(encoding="utf-8")
    missing = []
    for match in PROMPT_PATH_RE.finditer(text):
        candidate = match.group(0).replace("\\", "/").rstrip(".,;:)")
        if (REPO / candidate).exists():
            continue
        if any(candidate.startswith(prefix) for prefix in GENERATED):
            continue
        missing.append(candidate)
    assert missing == [], f"prompt.md names paths that are not here: {sorted(set(missing))}"


def test_the_paths_the_prompt_builds_were_not_shipped() -> None:
    """The other half of that rule: what the prompt's own steps produce is build
    output or a run's diagnostics, never source.  A working tree that has been built
    or used is fine - the skip list keeps those files out of the manifest - but
    nothing under those prefixes may be committed."""
    committed = [
        name
        for name in _manifest()
        if any(name.startswith(prefix) for prefix in GENERATED)
    ]
    assert committed == [], f"build output is committed here: {sorted(committed)}"
