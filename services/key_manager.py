"""
services/key_manager.py — Secure signing-key management for the keeper bot (BK-11)

The keeper's Stellar signing seed (an ed25519 secret, ``S...``) must never live in
plaintext in production. AWS KMS cannot sign ed25519 directly, so this module uses
**envelope encryption**: the seed is stored only as KMS-encrypted ciphertext (or in
HashiCorp Vault's KV store for local dev) and decrypted in memory at signing time.

Backends (``SIGNING_BACKEND`` env var):
  * ``aws_kms``  — production. ``ADMIN_SECRET_KEY_CIPHERTEXT`` holds the base64
                   KMS-encrypted seed; ``kms:Decrypt`` recovers it.
  * ``vault``    — local dev / self-hosted. Seed lives in Vault KV v2 at
                   ``VAULT_KEY_PATH`` under field ``VAULT_KEY_FIELD``.
  * ``env_key``  — tests / throwaway dev only. Seed read straight from
                   ``ADMIN_SECRET_KEY``.

Key rotation (:func:`rotate_signing_key`) is quarterly: a new KMS key + alias
``alias/aegis-keeper-YYYYQn`` is generated, the seed is re-wrapped under it, and the
new key id + hash are written to ``keeper_config`` in a single transaction so signing
never sees a window without an active key.
"""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

VALID_BACKENDS = ("aws_kms", "vault", "env_key")


class SigningKeyError(RuntimeError):
    """Raised when the signing seed cannot be resolved from the configured backend."""


class SigningKeyRevokedError(SigningKeyError):
    """Raised when the active signing key has been emergency-revoked."""


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def quarterly_key_alias(now: datetime | None = None) -> str:
    """Return the KMS key alias for the quarter containing ``now`` (UTC)."""
    now = now or datetime.now(timezone.utc)
    quarter = (now.month - 1) // 3 + 1
    return f"alias/aegis-keeper-{now.year}Q{quarter}"


def key_hash(public_key: str) -> str:
    """sha256 hex digest of a signing public key — the value persisted to keeper_config."""
    return hashlib.sha256(public_key.encode("utf-8")).hexdigest()


class SigningKeyManager:
    """Resolves the keeper signing seed from the configured secrets backend."""

    def __init__(self, backend: str | None = None) -> None:
        self.backend = (backend or _env("SIGNING_BACKEND", "env_key")).lower()
        if self.backend not in VALID_BACKENDS:
            raise SigningKeyError(
                f"Unknown SIGNING_BACKEND {self.backend!r}; "
                f"expected one of {VALID_BACKENDS}"
            )

    # -- seed resolution --------------------------------------------------

    def resolve_secret(self) -> str:
        """Return the plaintext Stellar seed (``S...``). Never log the result."""
        if self.backend == "aws_kms":
            secret = self._from_kms()
        elif self.backend == "vault":
            secret = self._from_vault()
        else:
            secret = _env("ADMIN_SECRET_KEY")
        if not secret:
            raise SigningKeyError(
                f"No signing seed available from backend {self.backend!r}"
            )
        return secret

    def _from_kms(self) -> str:
        ciphertext_b64 = _env("ADMIN_SECRET_KEY_CIPHERTEXT")
        if not ciphertext_b64:
            raise SigningKeyError(
                "ADMIN_SECRET_KEY_CIPHERTEXT is not set "
                "(required for SIGNING_BACKEND=aws_kms)"
            )
        import boto3

        client = boto3.client("kms", region_name=_env("AWS_REGION", "us-east-1"))
        try:
            resp = client.decrypt(CiphertextBlob=base64.b64decode(ciphertext_b64))
        except Exception as exc:
            raise SigningKeyError(f"KMS decrypt failed: {exc}") from exc
        return resp["Plaintext"].decode("utf-8").strip()

    def _from_vault(self) -> str:
        import hvac

        addr = _env("VAULT_ADDR", "http://127.0.0.1:8200")
        token = _env("VAULT_TOKEN")
        path = _env("VAULT_KEY_PATH", "aegis/keeper-signing-key")
        field = _env("VAULT_KEY_FIELD", "stellar_secret")
        client = hvac.Client(url=addr, token=token)
        try:
            read = client.secrets.kv.v2.read_secret_version(path=path)
            data = read["data"]["data"]
        except Exception as exc:
            raise SigningKeyError(f"Vault read failed at {path!r}: {exc}") from exc
        secret = data.get(field, "")
        if not secret:
            raise SigningKeyError(f"Vault secret at {path!r} has no field {field!r}")
        return secret.strip()

    # -- helpers --------------------------------------------------------

    def is_configured(self) -> bool:
        """True when the backend has enough config to attempt a signature."""
        if self.backend == "aws_kms":
            return bool(_env("ADMIN_SECRET_KEY_CIPHERTEXT"))
        if self.backend == "vault":
            return bool(_env("VAULT_TOKEN"))
        return bool(_env("ADMIN_SECRET_KEY"))

    def public_key(self) -> str:
        """Derive the ``G...`` public key from the resolved seed."""
        from stellar_sdk import Keypair

        return Keypair.from_secret(self.resolve_secret()).public_key


def assert_signing_allowed() -> None:
    """Raise :class:`SigningKeyRevokedError` if the active signing key was revoked."""
    from lib.database import get_active_signing_config

    active = get_active_signing_config()
    if active and active.get("revoked"):
        raise SigningKeyRevokedError(
            f"Signing key {active.get('key_id')!r} was revoked at "
            f"{active.get('revoked_at')}: {active.get('revoked_reason')}"
        )


def rotate_signing_key(now: datetime | None = None, actor: str = "cron") -> dict:
    """
    Idempotent quarterly rotation of the keeper signing key.

    Safe to run any day from cron: if the current quarter's alias is already the
    active key, this is a no-op. Otherwise a new KMS key + alias is created, the
    seed is re-wrapped under it, and ``keeper_config`` gets a new active row in a
    single transaction (:func:`lib.database.insert_signing_config`) so signing
    never sees a window without an active key.
    """
    from lib.database import get_active_signing_config, insert_signing_config

    manager = SigningKeyManager()
    now = now or datetime.now(timezone.utc)
    target_alias = quarterly_key_alias(now)
    active = get_active_signing_config()

    if active and active.get("key_id") == target_alias and not active.get("revoked"):
        return {
            "rotated": False,
            "reason": "current quarter alias already active",
            "key_id": target_alias,
            "actor": actor,
        }

    seed = manager.resolve_secret()
    from stellar_sdk import Keypair

    public_key = Keypair.from_secret(seed).public_key
    new_hash = key_hash(public_key)

    result: dict = {
        "rotated": True,
        "key_id": target_alias,
        "key_hash": new_hash,
        "backend": manager.backend,
        "rotated_at": now.isoformat(),
        "actor": actor,
    }

    if manager.backend == "aws_kms":
        import boto3

        client = boto3.client("kms", region_name=_env("AWS_REGION", "us-east-1"))
        key_meta = client.create_key(
            Description=f"Aegis keeper signing-seed wrapping key ({target_alias})",
            KeyUsage="ENCRYPT_DECRYPT",
        )
        new_key_id = key_meta["KeyMetadata"]["KeyId"]
        client.create_alias(AliasName=target_alias, TargetKeyId=new_key_id)
        wrapped = client.encrypt(KeyId=target_alias, Plaintext=seed.encode("utf-8"))
        result["kms_key_id"] = new_key_id
        result["new_ciphertext"] = base64.b64encode(wrapped["CiphertextBlob"]).decode(
            "ascii"
        )

    insert_signing_config(
        key_id=target_alias, key_hash=new_hash, backend=manager.backend
    )
    return result
