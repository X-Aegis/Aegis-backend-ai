# Key Management Runbook

This runbook covers the procedures for managing the cryptographic keys used by the Aegis backend Keeper Bot. The bot uses these keys to sign Stellar Soroban transactions for automated portfolio rebalancing.

## Overview

The Keeper Bot supports three signing backends:
- **`env_key`**: Standard local Stellar Secret Key (for local development and testing only).
- **`vault`**: HashiCorp Vault transit engine (for staging and advanced local setups).
- **`aws_kms`**: AWS Key Management Service (for production environments).

**Security Policy:** The keeper bot signing key must never exist in plaintext in production.

## 1. Setup Procedures

### Local Development (`env_key` or `vault`)

**Using `env_key` (Default):**
Set the `SIGNING_BACKEND=env_key` and provide `ADMIN_SECRET_KEY` in your `.env`.

**Using Vault:**
1. Ensure Vault is running locally (`vault server -dev`).
2. Enable the transit secrets engine: `vault secrets enable transit`
3. Create the key: `vault write -f transit/keys/admin-key type=ed25519`
4. Update `.env`:
   ```
   SIGNING_BACKEND=vault
   VAULT_ADDR=http://127.0.0.1:8200
   VAULT_TOKEN=<your_dev_token>
   VAULT_KEY_PATH=transit/sign/admin-key
   ```

### Production Setup (`aws_kms`)

1. Create a symmetric, asymmetric sign/verify KMS key (Ed25519) in AWS KMS.
2. Create an alias for the key (e.g., `alias/keeper-key-v1`).
3. Note the key ID or alias ARN.
4. Update environment variables in the production environment:
   ```
   SIGNING_BACKEND=aws_kms
   AWS_REGION=us-east-1
   AWS_KMS_KEY_ID=alias/keeper-key-v1
   ```
5. Ensure the IAM role attached to the Keeper Bot ECS task has `kms:Sign` and `kms:GetPublicKey` permissions for this specific key.

## 2. Key Rotation Procedure

Keys MUST be rotated on a **quarterly basis**.

### Rotation Steps

1. **Generate New Key:**
   - Log into the AWS Console / HashiCorp Vault.
   - Create a new Ed25519 key (e.g., `keeper-key-v2`).
   - If using KMS, create a new alias or update the existing alias to point to the new key.
   
2. **Update Keeper Config Database:**
   - The database tracks the active key to ensure we don't sign with revoked keys.
   - Run a SQL update to change the active key alias in `keeper_config`.
   ```sql
   UPDATE keeper_config 
   SET key_alias = 'alias/keeper-key-v2', rotation_timestamp = now(), status = 'active'
   WHERE id = (SELECT max(id) FROM keeper_config);
   ```
   - *Note: Automated rotation endpoints are under development.*

3. **Verify:**
   - Check the `audit_signing_log` table to ensure new transactions are being signed using the new `key_id`.

## 3. Emergency Revocation

If a key is suspected to be compromised, it must be revoked immediately. The Keeper Bot is designed to stop signing transactions if its key is revoked.

### Using the API

1. Ensure your IP address is in the `ALLOWED_KEEPER_IPS` environment variable list.
2. Send a POST request to the emergency endpoint:
   ```bash
   curl -X POST https://api.aegis.example.com/keeper/emergency-revocate \
        -H "X-Forwarded-For: <your-allowed-ip>"
   ```
3. The API will update the `keeper_config` table, setting the active key status to `revoked`.

### Manual Revocation

If the API is unreachable:

1. **Database:** Manually update the database.
   ```sql
   UPDATE keeper_config SET status = 'revoked';
   ```
2. **Infrastructure (KMS):** Disable the key in the AWS KMS Console.
3. **Infrastructure (Vault):** Delete the key or update policy to deny access.
