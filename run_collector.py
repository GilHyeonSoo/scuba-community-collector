"""PyInstaller entry point (absolute imports work in both source and frozen builds)."""

from collector.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
