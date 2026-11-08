"""``python -m lhai.mcp`` - same entry point as the ``lhai-mcp`` console script."""

from __future__ import annotations

import sys

from lhai.mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
