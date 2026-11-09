"""PyInstaller entry point for the frozen backend sidecar.

Equivalent to `python -m app.main`; kept separate so the frozen binary
has an unambiguous console entry.
"""

from app.main import main

if __name__ == "__main__":
    main()
