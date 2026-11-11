"""Dump AT-SPI top-level frames as JSON (fixture material for matching.py).
Run in the deckery distrobox: python3 tools/dump_frames.py OUT.json"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deckery_atspi.atspi_frames import list_frames

t0 = time.monotonic()
frames = list_frames()
print(f"{len(frames)} frames in {1000 * (time.monotonic() - t0):.0f} ms", file=sys.stderr)
json.dump([f.to_json() for f in frames], open(sys.argv[1], "w"), indent=1)
