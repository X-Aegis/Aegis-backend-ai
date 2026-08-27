"""Tests for the BK-11 secure-key-management helpers in lib/database.py.

The database is not available in CI, so these use a fake psycopg2 connection and
assert on the SQL / transaction shape (in particular that key rotation is atomic
and that the signing audit log is insert-only).
"""

import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.database as db


class FakeCursor:
    def __init__(self, rows=None, raise_on_execute=False):
        self.executed = []
        self._rows = rows or []
        self._raise = raise_on_execute

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self._raise:
            raise RuntimeError("boom")
        self.executed.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, rows=None, raise_on_execute=False):
        self.cur = FakeCursor(rows, raise_on_execute)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self, *args, **kwargs):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_conn(monkeypatch):
    def _install(rows=None, raise_on_execute=False):
        conn = FakeConn(rows, raise_on_execute)
        monkeypatch.setattr(db, "get_connection", lambda: conn)
        return conn

    return _install


# ---------------------------------------------------------------------------
# insert_signing_config — atomic deactivate-then-insert (no downtime)
# ---------------------------------------------------------------------------


def test_insert_signing_config_is_atomic(fake_conn):
    conn = fake_conn()
    db.insert_signing_config(
        key_id="alias/aegis-keeper-2026Q3", key_hash="h", backend="aws_kms"
    )

    sqls = [sql for sql, _ in conn.cur.executed]
    assert "UPDATE keeper_config SET active = FALSE WHERE active" in sqls[0]
    assert "INSERT INTO keeper_config" in sqls[1]
    # one commit for the whole rotation — never a window without an active key
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert conn.closed is True


def test_insert_signing_config_rolls_back_on_error(fake_conn):
    conn = fake_conn(raise_on_execute=True)
    with pytest.raises(RuntimeError):
        db.insert_signing_config(key_id="alias/x", key_hash="h", backend="vault")
    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert conn.closed is True


# ---------------------------------------------------------------------------
# audit_signing_log — insert only
# ---------------------------------------------------------------------------


def test_record_signing_event_inserts_only(fake_conn):
    conn = fake_conn()
    db.record_signing_event("txhash123", "alias/aegis-keeper-2026Q3", "keeper_bot")

    sql, params = conn.cur.executed[0]
    assert "INSERT INTO audit_signing_log" in sql
    assert params == ("txhash123", "alias/aegis-keeper-2026Q3", "keeper_bot")
    assert len(conn.cur.executed) == 1
    assert conn.commits == 1


def test_get_signing_audit_log_newest_first(fake_conn):
    rows = [{"id": 2}, {"id": 1}]
    conn = fake_conn(rows=rows)
    result = db.get_signing_audit_log(limit=50)

    sql, params = conn.cur.executed[0]
    assert "ORDER BY id DESC" in sql
    assert params == (50,)
    assert result == rows


# ---------------------------------------------------------------------------
# revoke_active_signing_key
# ---------------------------------------------------------------------------


def test_revoke_active_signing_key_sets_fields_and_returns_row(fake_conn):
    row = {"id": 1, "key_id": "alias/x", "revoked": True}
    conn = fake_conn(rows=[row])
    out = db.revoke_active_signing_key("suspected leak", actor="emergency-endpoint")

    sql, params = conn.cur.executed[0]
    assert "SET revoked = TRUE" in sql
    assert "revoked_at = now()" in sql
    assert "WHERE active" in sql
    assert params[0] == "suspected leak (revoked by emergency-endpoint)"
    assert out == row
    assert conn.commits == 1


def test_revoke_active_signing_key_returns_none_when_no_active_key(fake_conn):
    fake_conn(rows=[])
    assert db.revoke_active_signing_key("x", actor="y") is None


# ---------------------------------------------------------------------------
# get_active_signing_config
# ---------------------------------------------------------------------------


def test_get_active_signing_config_filters_active(fake_conn):
    row = {"id": 1, "key_id": "alias/x", "active": True, "revoked": False}
    conn = fake_conn(rows=[row])
    assert db.get_active_signing_config() == row
    sql, _ = conn.cur.executed[0]
    assert "WHERE active" in sql


def test_get_active_signing_config_none_when_unrotated(fake_conn):
    fake_conn(rows=[])
    assert db.get_active_signing_config() is None
