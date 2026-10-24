"""Regenerate the synthetic history cases PA-1011..PA-1040 (deterministic).

The ten hand-written cases PA-1001..PA-1010 (including hero cases A, B, C) are left untouched.
Run: python scripts/make_dataset.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from packs.health_preauth import tools  # noqa: E402
from packs.health_preauth.generator import static_history  # noqa: E402

if __name__ == "__main__":
    for req in static_history():
        path = tools.CASES / f"{req['case_id']}.json"
        path.write_text(json.dumps(req, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print("wrote", path.name, req["title"], "·", req["hospital"]["name"])
