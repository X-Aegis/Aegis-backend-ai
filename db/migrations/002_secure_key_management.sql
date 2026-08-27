-- db/migrations/002_secure_key_management.sql
--
-- Secure Key Management — Vault/KMS Integration (BK-11).
--
-- The keeper bot must never hold its Stellar signing seed in plaintext in
-- production. This migration adds the state and audit tables that back the
-- envelope-encryption signing flow (services/key_manager.py):
--
--   * keeper_config       — current + historical signing-key config. The seed
--                           is never stored; only the KMS alias / Vault path and
--                           the sha256 of the public key. Quarterly rotation
--                           inserts a new active row and deactivates the old one
--                           in one transaction (no downtime).
--   * audit_signing_log   — append-only record of every signing operation
--                           (timestamp, tx_hash, key_id, actor). UPDATE/DELETE
--                           are blocked by rules.
--
-- Safe to run on an existing database; idempotent.

BEGIN;

CREATE TABLE IF NOT EXISTS keeper_config (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    key_id         VARCHAR(255) NOT NULL,
    key_hash       VARCHAR(64)  NOT NULL,
    backend        VARCHAR(20)  NOT NULL,
    rotated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    active         BOOLEAN NOT NULL DEFAULT TRUE,
    revoked        BOOLEAN NOT NULL DEFAULT FALSE,
    revoked_at     TIMESTAMPTZ,
    revoked_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_keeper_config_one_active
ON keeper_config (active) WHERE active;

CREATE TABLE IF NOT EXISTS audit_signing_log (
    id          BIGSERIAL PRIMARY KEY,
    "timestamp" TIMESTAMPTZ NOT NULL DEFAULT now(),
    tx_hash     VARCHAR(128) NOT NULL,
    key_id      VARCHAR(255) NOT NULL,
    actor       VARCHAR(100) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_signing_log_timestamp
ON audit_signing_log ("timestamp" DESC);

CREATE OR REPLACE RULE audit_signing_log_no_update AS
ON UPDATE TO audit_signing_log DO INSTEAD NOTHING;

CREATE OR REPLACE RULE audit_signing_log_no_delete AS
ON DELETE TO audit_signing_log DO INSTEAD NOTHING;

COMMIT;
