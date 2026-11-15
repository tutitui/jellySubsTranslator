#!/usr/bin/env python3
"""Command-line entrypoint; shares providers and settings with the Jellyfin plugin."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent / 'worker'))
from cli import main

if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Translation stopped: {exc}. Saved progress is retained.')
        raise SystemExit(1)
