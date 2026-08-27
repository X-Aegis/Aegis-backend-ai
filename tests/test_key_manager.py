"""Tests for services/key_manager.py (BK-11 secure key management)."""

import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stellar_sdk import Keypair

import lib.database as db
import services.key_manager as km

ROOT = Path(__file__).resolve().parents[1]
SEED = Keypair.random().secret
PUBLIC = Keypair.from_secret(SEED).public_key


# ---------------------------------------------------------------------------
# quarterly_key_alias / key_hash
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "month, expected_q",
    [(1, 1), (3, 1), (4, 2), (6, 2), (7, 3), (9, 3), (10, 4), (12, 4)],
)
def test_quarterly_key_alias(month, expected_q):
    now = datetime(2026, month, 15, tzinfo=timezone.utc)
    assert km.quarterly_key_alias(now) == f"alias/aegis-keeper-2026Q{expected_q}"


def test_key_hash_is_deterministic_sha256():
    h1 = km.key_hash(PUBLIC)
    h2 = km.key_hash(PUBLIC)
    assert h1 == h2
    assert len(h1) == 64
    assert km.key_hash("G...other") != h1


# ---------------------------------------------------------------------------
# SigningKeyManager.resolve_secret
# ---------------------------------------------------------------------------


def test_unknown_backend_rejected():
    with pytest.raises(km.SigningKeyError, match="Unknown SIGNING_BACKEND"):
        km.SigningKeyManager("nonsense")


def test_env_key_backend_resolves_from_env(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET_KEY", SEED)
    mgr = km.SigningKeyManager("env_key")
    assert mgr.resolve_secret() == SEED
    assert mgr.public_key() == PUBLIC
    assert mgr.is_configured() is True


def test_env_key_backend_raises_when_missing(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET_KEY", raising=False)
    with pytest.raises(km.SigningKeyError, match="No signing seed"):
        km.SigningKeyManager("env_key").resolve_secret()


def test_vault_backend_reads_kv_v2(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")
    monkeypatch.setenv("VAULT_KEY_PATH", "aegis/keeper-signing-key")

    read_calls = {}

    class FakeKvV2:
        def read_secret_version(self, path):
            read_calls["path"] = path
            return {"data": {"data": {"stellar_secret": SEED}}}

    class FakeClient:
        def __init__(self, url, token):
            read_calls["url"] = url
            read_calls["token"] = token
            self.secrets = Mock()
            self.secrets.kv.v2 = FakeKvV2()

    monkeypatch.setattr("hvac.Client", FakeClient)

    mgr = km.SigningKeyManager("vault")
    assert mgr.resolve_secret() == SEED
    assert read_calls["path"] == "aegis/keeper-signing-key"
    assert read_calls["token"] == "dev-token"


def test_vault_backend_missing_field_raises(monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "dev-token")

    class FakeClient:
        def __init__(self, url, token):
            self.secrets = Mock()
            self.secrets.kv.v2.read_secret_version.return_value = {
                "data": {"data": {"wrong_field": "x"}}
            }

    monkeypatch.setattr("hvac.Client", FakeClient)
    with pytest.raises(km.SigningKeyError, match="no field"):
        km.SigningKeyManager("vault").resolve_secret()


def test_aws_kms_backend_decrypts_ciphertext(monkeypatch):
    monkeypatch.setenv(
        "ADMIN_SECRET_KEY_CIPHERTEXT",
        base64.b64encode(b"wrapped-bytes").decode("ascii"),
    )
    fake_kms = Mock()
    fake_kms.decrypt.return_value = {"Plaintext": SEED.encode("utf-8")}
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake_kms)

    mgr = km.SigningKeyManager("aws_kms")
    assert mgr.resolve_secret() == SEED
    fake_kms.decrypt.assert_called_once_with(CiphertextBlob=b"wrapped-bytes")


def test_aws_kms_backend_requires_ciphertext(monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET_KEY_CIPHERTEXT", raising=False)
    with pytest.raises(km.SigningKeyError, match="ADMIN_SECRET_KEY_CIPHERTEXT"):
        km.SigningKeyManager("aws_kms").resolve_secret()


# ---------------------------------------------------------------------------
# assert_signing_allowed — revocation blocks signing
# ---------------------------------------------------------------------------


def test_assert_signing_allowed_passes_when_not_revoked(monkeypatch):
    monkeypatch.setattr(
        "lib.database.get_active_signing_config",
        lambda: {"key_id": "alias/x", "revoked": False},
    )
    km.assert_signing_allowed()  # should not raise


def test_assert_signing_allowed_passes_when_no_config(monkeypatch):
    monkeypatch.setattr("lib.database.get_active_signing_config", lambda: None)
    km.assert_signing_allowed()


def test_assert_signing_allowed_raises_when_revoked(monkeypatch):
    monkeypatch.setattr(
        "lib.database.get_active_signing_config",
        lambda: {
            "key_id": "alias/aegis-keeper-2026Q3",
            "revoked": True,
            "revoked_at": "2026-08-27T00:00:00Z",
            "revoked_reason": "leak",
        },
    )
    with pytest.raises(km.SigningKeyRevokedError, match="was revoked"):
        km.assert_signing_allowed()


# ---------------------------------------------------------------------------
# rotate_signing_key
# ---------------------------------------------------------------------------


def test_rotate_is_noop_when_current_quarter_alias_active(monkeypatch):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "lib.database.get_active_signing_config",
        lambda: {"key_id": "alias/aegis-keeper-2026Q3", "revoked": False},
    )
    inserted = []
    monkeypatch.setattr(
        "lib.database.insert_signing_config",
        lambda **kw: inserted.append(kw),
    )
    result = km.rotate_signing_key(now=now)
    assert result["rotated"] is False
    assert inserted == []


def test_rotate_env_key_backend_writes_new_config(monkeypatch):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("SIGNING_BACKEND", "env_key")
    monkeypatch.setenv("ADMIN_SECRET_KEY", SEED)
    monkeypatch.setattr("lib.database.get_active_signing_config", lambda: None)
    inserted = {}
    monkeypatch.setattr(
        "lib.database.insert_signing_config", lambda **kw: inserted.update(kw)
    )

    result = km.rotate_signing_key(now=now, actor="cron")

    assert result["rotated"] is True
    assert result["key_id"] == "alias/aegis-keeper-2026Q3"
    assert inserted["key_id"] == "alias/aegis-keeper-2026Q3"
    assert inserted["key_hash"] == km.key_hash(PUBLIC)
    assert inserted["backend"] == "env_key"


def test_rotate_aws_kms_creates_key_and_rewraps(monkeypatch):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setenv("SIGNING_BACKEND", "aws_kms")
    monkeypatch.setenv(
        "ADMIN_SECRET_KEY_CIPHERTEXT",
        base64.b64encode(b"old-wrapped").decode("ascii"),
    )
    monkeypatch.setattr("lib.database.get_active_signing_config", lambda: None)
    monkeypatch.setattr("lib.database.insert_signing_config", lambda **kw: None)

    fake_kms = Mock()
    fake_kms.decrypt.return_value = {"Plaintext": SEED.encode("utf-8")}
    fake_kms.create_key.return_value = {"KeyMetadata": {"KeyId": "kms-key-123"}}
    fake_kms.encrypt.return_value = {"CiphertextBlob": b"new-wrapped"}
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake_kms)

    result = km.rotate_signing_key(now=now)

    fake_kms.create_alias.assert_called_once_with(
        AliasName="alias/aegis-keeper-2026Q3", TargetKeyId="kms-key-123"
    )
    assert result["kms_key_id"] == "kms-key-123"
    assert base64.b64decode(result["new_ciphertext"]) == b"new-wrapped"


# ---------------------------------------------------------------------------
# audit log immutability (schema + migration + no mutating helper)
# ---------------------------------------------------------------------------


def test_audit_signing_log_is_immutable_in_schema():
    schema = (ROOT / "db" / "schema.sql").read_text(encoding="utf-8")
    migration = (
        ROOT / "db" / "migrations" / "002_secure_key_management.sql"
    ).read_text(encoding="utf-8")
    for text in (schema, migration):
        assert "audit_signing_log_no_update" in text
        assert "audit_signing_log_no_delete" in text
        assert text.count("DO INSTEAD NOTHING") >= 2


def test_no_mutating_helper_for_signing_audit():
    assert hasattr(db, "record_signing_event")
    assert hasattr(db, "get_signing_audit_log")
    for name in dir(db):
        assert not (name.startswith(("update_", "delete_")) and "signing" in name)
