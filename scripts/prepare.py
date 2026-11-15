#!/usr/bin/env python3
"""Create private worker settings and a shared Jellyfin/worker bridge token."""
import json
import os
import secrets
from pathlib import Path
root = Path(__file__).resolve().parents[1]
runtime = root / 'runtime'
runtime.mkdir(mode=0o700, exist_ok=True)
os.chmod(runtime, 0o700)
env = runtime / 'bridge.env'
if not env.exists():
    env.write_text('SUBTITLE_TRANSLATOR_TOKEN=' + secrets.token_hex(32) + '\n')
os.chmod(env, 0o600)
settings = runtime / 'settings.json'
if not settings.exists():
    settings.write_text(json.dumps({'provider': 'openai', 'providers': {}}))
os.chmod(settings, 0o600)
print('Private runtime settings prepared. Configure provider keys in the Jellyfin dashboard.')
