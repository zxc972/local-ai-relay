"""Cross-platform installed console entry point."""
from __future__ import annotations

import sys

from .core import RelayError
from .relay import dump, main as run


def main():
    try:
        run(["pair", "work"] if len(sys.argv) == 1 else None)
    except (RelayError, OSError, ValueError) as error:
        dump({"error": str(error)})
        return 2
    return 0
