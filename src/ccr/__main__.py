"""Module entrypoint so `python -m ccr` dispatches into the argparse CLI."""

from __future__ import annotations

from ccr.cli import main

if __name__ == "__main__":
    main()
