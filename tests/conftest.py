import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The suite always runs offline and deterministic.
os.environ["LLM_MODE"] = "mock"
os.environ.setdefault("SEED_HISTORY", "false")
os.environ["REPLAY_RECORD"] = "false"
os.environ["LIVE_FEED"] = "false"  # tests drive the feed with Feed.tick()
