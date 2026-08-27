# Runbook: Keeper signing-key management (BK-11)

Component: `services/key_manager.py`, `services/keeper_bot.py`, `api/keeper.py`
Tables: `keeper_config`, `audit_signing_log`
Severity of a key incident: **CRITICAL** — a compromised signing key can drain or
mis-allocate the vault. Never paste a seed (`S...`), a KMS ciphertext, or a Vault
token into Slack, tickets, or logs.

## 1. Architecture

The keeper signs Soroban `rebalance` transactions with a Stellar **ed25519** seed.
AWS KMS cannot sign ed25519 directly, so the seed is protected by **envelope
encryption**:

| Backend (`SIGNING_BACKEND`) | Where the seed lives | How signing gets it |
| --- | --- | --- |
| `aws_kms` (production) | `ADMIN_SECRET_KEY_CIPHERTEXT` — base64 of the KMS-encrypted seed. Plaintext is never stored or written to disk. | `kms:Decrypt` in memory at signing time |
| `vault` (dev / self-hosted) | HashiCorp Vault KV v2 at `VAULT_KEY_PATH`, field `VAULT_KEY_FIELD` | `hvac` read at signing time |
| `env_key` (tests only) | `ADMIN_SECRET_KEY` env var, plaintext | direct read |

`keeper_config` holds only the **key id** (KMS alias / Vault path) and the
**sha256 of the signing public key** — never the seed. `audit_signing_log` records
every signing operation `(timestamp, tx_hash, key_id, actor)` and is append-only
(UPDATE/DELETE blocked by database rules).

## 2. Incident description

Triggers for this runbook:

- Suspected key compromise (leaked seed / ciphertext / Vault token, anomalous
  `audit_signing_log` entries, unexpected on-chain `rebalance`).
- Quarterly rotation that did not run or failed.
- Keeper logging `Signing key revoked — refusing to sign rebalance`.
- KMS / Vault outage blocking signing (`SigningKeyError` in keeper logs).

## 3. Investigation steps

1. Identify the active key:
   ```sql
   SELECT id, key_id, key_hash, backend, rotated_at, revoked, revoked_at, revoked_reason
   FROM keeper_config WHERE active;
   ```
2. Review recent signing activity for unexpected `tx_hash` / `actor`:
   ```sql
   SELECT * FROM audit_signing_log ORDER BY id DESC LIMIT 50;
   ```
   or `GET /keeper/signing-audit` (admin token required).
3. Cross-check each `tx_hash` on the network for `SOROBAN_NETWORK_PASSPHRASE`.
4. Confirm the backend is healthy: `SIGNING_BACKEND`, KMS key state
   (`aws kms describe-key --key-id alias/aegis-keeper-<quarter>`), or Vault
   (`vault kv get <VAULT_KEY_PATH>`).
5. Check `GET /keeper/status` and `runbooks/rebalance-failed.md` if signing
   failures are also tripping the circuit breaker.

## 4. Escalation path

| Time / condition | Who |
| --- | --- |
| T+0 suspected compromise | On-call DevOps — revoke immediately (section 5), page `@bbkenny` |
| KMS / Vault outage | Platform / secrets owner; do not paste keys or tokens |
| Unexpected on-chain `rebalance` | Soroban contract maintainer + `@bbkenny` before any manual invoke |
| Rotation failed 2 quarters running | Platform owner — manual rotation (section 6) |

## 5. Remediation actions — emergency revocation

1. Revoke the active key (blocks all further keeper signing immediately):
   ```
   POST /keeper/emergency-revocate
   Headers: x-admin-token: <KEEPER_ADMIN_TOKEN>   (from an allowlisted IP)
   Body:    {"reason": "suspected seed leak in incident INC-1234"}
   ```
   This sets `keeper_config.revoked = TRUE`; `assert_signing_allowed()` then
   raises on every keeper cycle and the bot records a `rejected` decision instead
   of signing.
2. Best-effort disable the KMS key (`aws kms disable-key --key-id <kms_key_id>`).
   The endpoint attempts this automatically; verify it.
3. Confirm the keeper is refusing to sign — next `run_once` logs
   `Signing key revoked` and `GET /keeper/stats` shows a `rejected` decision.
4. Generate a **new** Stellar keypair offline. Update the on-chain `rebalance`
   authorization / admin address to the new public key.
5. Store the new seed: `aws kms encrypt` it under a fresh quarterly key and set
   `ADMIN_SECRET_KEY_CIPHERTEXT` (or write it to Vault). Never keep the plaintext.
6. Rotate the config in (section 6) so a non-revoked active row exists again.
7. Post an incident timeline; open a follow-up to audit how the key leaked.

## 6. Remediation actions — key rotation (no downtime)

Automatic: `scripts/rotate_keeper_key.py` runs quarterly from cron. Manual /
on-demand:

```
POST /keeper/rotate-key
Headers: x-admin-token: <KEEPER_ADMIN_TOKEN>   (from an allowlisted IP)
```

Rotation:

1. Computes this quarter's alias `alias/aegis-keeper-YYYYQn`. If it is already
   the active `key_id`, it is a **no-op**.
2. For `aws_kms`: creates a new KMS key + alias and re-wraps the current seed
   under it. The response includes `new_ciphertext` — set it as
   `ADMIN_SECRET_KEY_CIPHERTEXT` and redeploy **before** disabling the old key.
3. Writes the new `key_id` + `key_hash` to `keeper_config` in a single
   transaction that deactivates the previous row and inserts the new active row —
   there is never a moment with zero active keys, so the keeper never blocks.

Rollback: `UPDATE keeper_config SET active = TRUE WHERE id = <previous_id>` and
re-point `ADMIN_SECRET_KEY_CIPHERTEXT` at the previous ciphertext (only if that
key is not compromised).

## 7. IP allowlisting for key access

`POST /keeper/emergency-revocate` and `POST /keeper/rotate-key` require both a
valid `x-admin-token` **and** an allowlisted source IP.

- `KEY_ACCESS_IP_ALLOWLIST` — comma-separated IPs / CIDRs. Empty disables the
  check (local dev only — never in production).
- `KEY_ACCESS_TRUST_FORWARDED_FOR` — set to `true` only when the API sits behind
  a trusted reverse proxy / LB; the first `X-Forwarded-For` entry is then used as
  the client IP, otherwise the socket peer IP is used.

Add the operator bastion / VPN egress ranges only. Review the allowlist whenever
infrastructure changes.

## 8. Audit log

`audit_signing_log` is append-only: `CREATE RULE ... DO INSTEAD NOTHING` blocks
UPDATE and DELETE, and `lib/database.py` exposes only `record_signing_event`
(insert) and `get_signing_audit_log` (read). To retain history beyond the
operational window, ship rows to cold storage with a read-only role — do not
prune the table in place.
