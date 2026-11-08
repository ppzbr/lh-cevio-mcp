"""LH AI Studio - a unified, config-driven trainer for SVS / SVC / TTS.

Design invariants (enforced by review, not by the type system):

1. ``lhai.core`` depends on the standard library only.  Data tooling, the
   LH-Score compiler and the CLI must keep working on a machine with no CUDA.
2. Nothing outside ``lhai.cli`` may hard-code a concrete model class.  Every
   model, loss, vocoder and exporter is resolved through :mod:`lhai.registry`.
3. The acoustic model and the vocoder are *siblings*, never a monolith.  A
   checkpoint must stay loadable after swapping either one.

Import order for the public API::

    from lhai import Batch, LHSample, ModelOutput        # contracts
    from lhai.registry import ACOUSTIC, VOCODER, build   # plugin lookup

The contracts are re-exported lazily (PEP 562) so ``import lhai`` stays cheap
and never drags in the training stack.
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0"

PROJECT_NAME = "LH AI Studio"

#: Version of the LH-Score intermediate representation.
#: Policy: adding an *optional* field bumps the minor (older readers stay
#: correct); changing the meaning or unit of an existing field bumps the major.
#: A reader accepts any older minor silently, and warns on a newer minor because
#: the file may carry fields it does not know about.
LHSCORE_VERSION = "1.1"

_LAZY: dict[str, str] = {
    "Note": "lhai.core.types",
    "LHSample": "lhai.core.types",
    "Batch": "lhai.core.types",
    "Conditioning": "lhai.core.types",
    "ModelOutput": "lhai.core.types",
    "LossOutput": "lhai.core.types",
    "InferResult": "lhai.core.types",
    "FrontendOut": "lhai.core.types",
    "AlignmentResult": "lhai.core.types",
    "TASKS": "lhai.core.types",
    "TASK_TO_ID": "lhai.core.types",
    "ID_TO_TASK": "lhai.core.types",
    "LHStudioError": "lhai.core.errors",
    "LHScoreError": "lhai.core.errors",
    "ConfigError": "lhai.core.errors",
    "RegistryError": "lhai.core.errors",
}

__all__ = ["__version__", "PROJECT_NAME", "LHSCORE_VERSION", *_LAZY]


def __getattr__(name: str) -> Any:
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_path), name)


def __dir__() -> list[str]:
    return sorted({*globals(), *_LAZY})
