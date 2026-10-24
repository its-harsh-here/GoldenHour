from core.audit import GENESIS, AuditLog
from core.store import Store


def make(tmp_path, n=5) -> AuditLog:
    log = AuditLog(Store(tmp_path / "a.db"))
    for i in range(n):
        log.append(case_id=f"C{i}", actor="tester", role="PROCESSOR", action="approve" if i == 2 else "note", details={"amount": 1000 * i})
    return log


def test_chain_links_and_verifies(tmp_path):
    log = make(tmp_path)
    blocks = log.blocks()
    assert blocks[0]["prev_hash"] == GENESIS
    assert all(b["prev_hash"] == a["hash"] for a, b in zip(blocks, blocks[1:]))
    result = log.verify()
    assert result["valid"] and result["checked"] == 5 and result["broken_at"] is None


def test_tamper_is_detected_at_that_block_then_restored(tmp_path):
    log = make(tmp_path)
    tampered = log.tamper()
    assert tampered["tampered_block"] == 3  # the approve block
    result = log.verify()
    assert not result["valid"] and result["broken_at"] == 3
    assert "hash" in result["reason"]
    assert [b["ok"] for b in result["blocks"]] == [True, True, False, False, False]
    log.restore()
    assert log.verify()["valid"]


def test_deleted_block_breaks_the_link(tmp_path):
    log = make(tmp_path)
    log.store.execute("DELETE FROM audit_log WHERE id=2")
    result = log.verify()
    assert not result["valid"] and result["broken_at"] == 3 and "link" in result["reason"]


def test_empty_chain_is_valid(tmp_path):
    assert AuditLog(Store(tmp_path / "e.db")).verify()["valid"]
