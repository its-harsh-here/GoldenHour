"""Guards against shipping a build without the pack's data (it happened once: .gitignore hid packs/*/data/)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "packs" / "health_preauth" / "data"


def _in_git_checkout() -> bool:
    return shutil.which("git") is not None and (ROOT / ".git").exists()


@pytest.mark.skipif(not _in_git_checkout(), reason="needs a git checkout")
def test_pack_data_is_tracked_by_git():
    needed = [p.relative_to(ROOT).as_posix() for p in sorted(DATA.rglob("*")) if p.is_file()]
    assert len(needed) >= 15  # policies, procedures, replay cache, 10 cases, 2 PDFs
    tracked = set(subprocess.run(["git", "ls-files", "packs/health_preauth/data"], cwd=ROOT, capture_output=True, text=True).stdout.split())
    missing = [p for p in needed if p not in tracked]
    assert not missing, f"pack data not committed (check .gitignore): {missing}"


def test_replay_cache_has_recorded_runs():
    import json

    assert len(json.loads((DATA / "replay_cache.json").read_text(encoding="utf-8"))["entries"]) > 0
