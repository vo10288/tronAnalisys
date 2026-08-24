#!/usr/bin/env python3
"""Compatibility entry point for users of the original single-file project."""
from tronanalysis.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
