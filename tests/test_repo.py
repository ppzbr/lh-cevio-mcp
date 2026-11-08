"""This repository must be self-describing and internally consistent.

Three kinds of failure this file exists to catch, all of which have happened in
the project this bridge came from:

* a shipped DLL that does not match the `build-info.txt` next to it, so the
  installer's runtime check passes on a binary nobody built (the manifest and
  the build info are the two records of provenance, and they must agree);
* a manifest that has drifted from the working tree, so a hand-edited file ships
  under a stale hash;
* a test run that exercises a `lhai` installed somewhere else, which reports
  green for code this repository does not contain.

It also asserts that the files `prompt.md` tells an agent to run are present:
a prompt that names a path which does not exist is worse than no prompt.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MANIFEST = REPO / "MANIFEST.txt"

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "build",
    ".mypy_cache",
    ".ruff_cache",
}
SKIP_SUFFIXES = (".pyc", ".pyo", ".lib", ".exp", ".pdb")
# `pip install -e .` writes these into the project root; they are build output.
SKIP_ENDINGS = (".egg-info", ".dist-info")
SKIP_NAMES = {
    "MANIFEST.txt",
    "LhCeVioBridge.boot.txt",
    "LhCeVioBridge.status.json",
    "LhCeVioLoader.boot.txt",
    "LhCeVioLoader.target.txt",
}


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
        out.add(rel.as_posix())
    return out


def _manifest() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([0-9a-f]{64})\s+(\S+)$", line)
        if match:
            entries[match.group(2)] = match.group(1)
    return entries


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
def _build_info() -> dict[str, str]:
    text = (REPO / "bridges/cevio/LhCeVioBridge.build-info.txt").read_text(encoding="utf-8")
    info = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            info[key.strip()] = value.strip()
    return info


def test_the_bridge_dll_is_the_one_the_build_info_describes() -> None:
    info = _build_info()
    assert _hash(REPO / "bridges/cevio/LhCeVioBridge.dll").upper() == info["Sha256"].upper()


def test_the_loader_dll_is_the_one_the_build_info_describes() -> None:
    info = _build_info()
    assert info["Loader"] == "LhCeVioLoader.dll"
    assert _hash(REPO / "bridges/cevio/native/LhCeVioLoader.dll").upper() == info["LoaderSha256"].upper()


def test_the_bridge_targets_the_runtime_cevio_actually_is() -> None:
    """CeVIO AI is a .NET Framework 4.8 process; a bridge built for anything else
    loads, fails to resolve, and gives no clue why.  install.ps1 checks this
    before copying - this checks the file that ships."""
    assert _build_info()["TargetFramework"] == "net48"
    assert _build_info()["Platform"] == "x64"


# --------------------------------------------------------------------------- #
# the tree is the one being tested
# --------------------------------------------------------------------------- #
def test_the_client_under_test_is_this_repository() -> None:
    """A `lhai` installed from somewhere else would make every other test a
    statement about a different tree."""
    import lhai

    assert Path(lhai.__file__).resolve().parents[1] == REPO


# --------------------------------------------------------------------------- #
# the prompt describes files that exist
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "relative",
    [
        "bridges/cevio/build.ps1",
        "bridges/cevio/inject.py",
        "bridges/cevio/install.ps1",
        "bridges/cevio/uninstall.ps1",
        "bridges/cevio/LhCeVioBridge.dll",
        "bridges/cevio/native/LhCeVioLoader.dll",
        "lhai/live/inject.py",
        "lhai/mcp/server.py",
    ],
)
def test_the_files_the_prompt_names_are_present(relative: str) -> None:
    assert (REPO / relative).is_file(), f"prompt.md names {relative}, which is not here"


def test_the_prompt_is_self_contained() -> None:
    """The prompt is pasted into an agent that has never seen this repository, so
    it must not depend on a file it does not tell the agent to read."""
    text = (REPO / "prompt.md").read_text(encoding="utf-8")
    assert text.count("---") >= 1
    # Every backticked path that looks like a repository path must exist.
    for match in re.finditer(r"`((?:bridges|lhai|tests|docs|tools)/[\w./-]+)`", text):
        assert (REPO / match.group(1)).exists(), f"prompt.md names {match.group(1)}"
