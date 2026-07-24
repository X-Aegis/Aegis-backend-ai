"""
Keeper Bot — Off-chain rebalancer worker (Issue #BK-5).

Polls the Model API every hour. If the current volatility score crosses
the configured threshold, it builds a `rebalance` transaction, signs it
via AWS KMS or HashiCorp Vault, and submits it to the Soroban RPC endpoint.
"""

import os
import sys
import json
import time
import base64
import logging
import hashlib
import hmac
from datetime import datetime, timezone

import httpx

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import save_rebalance_event

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("keeper_bot")

# ---------------------------------------------------------------------------
# Configuration (all values come from environment variables)
# ---------------------------------------------------------------------------

# Model API
MODEL_API_BASE_URL = os.getenv("MODEL_API_BASE_URL", "http://localhost:8000")
RISK_HORIZON = int(os.getenv("RISK_HORIZON", "1"))  # hours ahead

# Rebalance threshold: if volatility_score >= this value → shift to stable
REBALANCE_THRESHOLD = float(os.getenv("REBALANCE_THRESHOLD", "80.0"))

# Poll interval in seconds (default 1 hour)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "3600"))

# Soroban RPC
SOROBAN_RPC_URL = os.getenv("SOROBAN_RPC_URL", "https://soroban-testnet.stellar.org")
CONTRACT_ID = os.getenv("SOROBAN_CONTRACT_ID", "")  # Stellar/Soroban contract address
NETWORK_PASSPHRASE = os.getenv(
    "STELLAR_NETWORK_PASSPHRASE", "Test SDF Network ; September 2015"
)

# Signing backend: "kms" | "vault" | "env_key" (dev/test fallback)
SIGNER_BACKEND = os.getenv("SIGNER_BACKEND", "env_key").lower()

# AWS KMS (used when SIGNER_BACKEND=kms)
AWS_KMS_KEY_ID = os.getenv("AWS_KMS_KEY_ID", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# HashiCorp Vault (used when SIGNER_BACKEND=vault)
VAULT_ADDR = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN = os.getenv("VAULT_TOKEN", "")
VAULT_KEY_PATH = os.getenv("VAULT_KEY_PATH", "transit/sign/admin-key")

# Fallback raw secret key (dev only — never use in production)
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "")


# ---------------------------------------------------------------------------
# Step 1: Poll the Model API
# ---------------------------------------------------------------------------

def fetch_current_risk() -> dict:
    """
    Calls GET /risk/current and returns the parsed JSON body.
    Raises on HTTP or connection errors so the caller can handle retry logic.
    """
    url = f"{MODEL_API_BASE_URL}/risk/current"
    params = {"horizon": RISK_HORIZON}
    with httpx.Client(timeout=15) as client:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Step 2: Decide whether a rebalance is needed
# ---------------------------------------------------------------------------

def needs_rebalance(risk_payload: dict, current_allocation: str) -> tuple[bool, str]:
    """
    Returns (should_rebalance, target_allocation).

    current_allocation: "risky" | "stable"
    target_allocation : "risky" | "stable"

    A rebalance is triggered only when the desired allocation differs from the
    current one — avoiding unnecessary on-chain transactions.
    """
    score = float(risk_payload["volatility_score"])
    risk_level = risk_payload.get("risk_level", "")

    target = "stable" if score >= REBALANCE_THRESHOLD else "risky"
    should = target != current_allocation

    log.info(
        "Risk check — score=%.2f threshold=%.2f risk_level=%s "
        "current=%s target=%s rebalance=%s",
        score,
        REBALANCE_THRESHOLD,
        risk_level,
        current_allocation,
        target,
        should,
    )
    return should, target


# ---------------------------------------------------------------------------
# Step 3: Build the rebalance transaction payload
# ---------------------------------------------------------------------------

def build_rebalance_transaction(target_allocation: str, volatility_score: float) -> dict:
    """
    Constructs the transaction envelope that will be signed and submitted to
    Soroban.  The structure follows the Soroban RPC `sendTransaction` format.

    In a full implementation this would use the stellar-sdk to build a proper
    XDR envelope; here we produce the structured payload that a Soroban
    invocation expects, ready to be serialised and signed.
    """
    tx = {
        "contract_id": CONTRACT_ID,
        "method": "rebalance",
        "args": {
            "allocation": target_allocation,      # "stable" | "risky"
            "volatility_score": volatility_score,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "network_passphrase": NETWORK_PASSPHRASE,
    }
    log.info("Built rebalance transaction: %s", json.dumps(tx))
    return tx


# ---------------------------------------------------------------------------
# Step 4: Sign the transaction
# ---------------------------------------------------------------------------

def _sign_with_aws_kms(payload_bytes: bytes) -> str:
    """Signs payload_bytes using AWS KMS and returns a base64-encoded signature."""
    try:
        import boto3  # imported lazily — only required when SIGNER_BACKEND=kms

        kms = boto3.client("kms", region_name=AWS_REGION)
        response = kms.sign(
            KeyId=AWS_KMS_KEY_ID,
            Message=payload_bytes,
            MessageType="RAW",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        signature = base64.b64encode(response["Signature"]).decode()
        log.info("Transaction signed via AWS KMS (key_id=%s)", AWS_KMS_KEY_ID)
        return signature
    except (KeyError, RuntimeError, OSError) as exc:
        log.error("AWS KMS signing failed: %s", exc)
        raise


def _sign_with_vault(payload_bytes: bytes) -> str:
    """Signs payload_bytes using HashiCorp Vault Transit and returns a base64-encoded signature."""
    try:
        input_b64 = base64.b64encode(payload_bytes).decode()
        url = f"{VAULT_ADDR}/v1/{VAULT_KEY_PATH}"
        headers = {"X-Vault-Token": VAULT_TOKEN, "Content-Type": "application/json"}
        body = {"input": input_b64}

        with httpx.Client(timeout=15) as client:
            response = client.post(url, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()

        # Vault returns "vault:v1:<base64_sig>"
        sig_raw = data["data"]["signature"]
        signature = sig_raw.split(":")[-1]  # strip vault prefix
        log.info("Transaction signed via HashiCorp Vault (%s)", VAULT_KEY_PATH)
        return signature
    except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError) as exc:
        log.error("HashiCorp Vault signing failed: %s", exc)
        raise


def _sign_with_env_key(payload_bytes: bytes) -> str:
    """
    Dev/test fallback: signs with ADMIN_SECRET_KEY using HMAC-SHA256.
    NOT suitable for production.
    """
    if not ADMIN_SECRET_KEY:
        raise OSError(
            "ADMIN_SECRET_KEY is not set. Configure a signing backend "
            "(SIGNER_BACKEND=kms or vault) for production use."
        )
    sig = hmac.new(ADMIN_SECRET_KEY.encode(), payload_bytes, hashlib.sha256).digest()
    signature = base64.b64encode(sig).decode()
    log.warning(
        "Transaction signed with env ADMIN_SECRET_KEY (dev fallback — "
        "do NOT use in production)"
    )
    return signature


def sign_transaction(transaction: dict) -> str:
    """
    Serialises the transaction to canonical JSON bytes and signs it using the
    configured backend (kms | vault | env_key).

    Returns a base64-encoded signature string.
    """
    payload_bytes = json.dumps(transaction, sort_keys=True).encode()

    if SIGNER_BACKEND == "kms":
        return _sign_with_aws_kms(payload_bytes)
    if SIGNER_BACKEND == "vault":
        return _sign_with_vault(payload_bytes)
    # Default / dev fallback
    return _sign_with_env_key(payload_bytes)


# ---------------------------------------------------------------------------
# Step 5: Submit to Soroban RPC
# ---------------------------------------------------------------------------

def submit_to_soroban(transaction: dict, signature: str) -> dict:
    """
    Submits the signed transaction to the Soroban RPC endpoint
    (sendTransaction JSON-RPC method).

    Returns the RPC response body.
    """
    if not SOROBAN_RPC_URL:
        raise OSError("SOROBAN_RPC_URL is not configured.")

    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": {
            "transaction": transaction,
            "signature": signature,
        },
    }

    log.info("Submitting transaction to Soroban RPC: %s", SOROBAN_RPC_URL)
    with httpx.Client(timeout=30) as client:
        response = client.post(SOROBAN_RPC_URL, json=rpc_payload)
        response.raise_for_status()
        result = response.json()

    if "error" in result:
        log.error("Soroban RPC error: %s", result["error"])
        raise RuntimeError(f"Soroban RPC returned an error: {result['error']}")

    log.info("Soroban RPC response: %s", json.dumps(result))
    return result


# ---------------------------------------------------------------------------
# Main rebalance cycle
# ---------------------------------------------------------------------------

def run_rebalance_cycle(current_allocation: str) -> str:
    """
    Executes one full rebalance cycle:
      1. Fetch current risk score from the Model API.
      2. Decide whether a rebalance is needed.
      3. If yes: build → sign → submit.
      4. Persist the outcome to the database.

    Returns the updated current_allocation for the next cycle.
    """
    log.info("=== Rebalance cycle started ===")

    # --- 1. Fetch risk ---
    try:
        risk = fetch_current_risk()
    except httpx.HTTPStatusError as exc:
        log.error("Model API HTTP error %s: %s", exc.response.status_code, exc)
        return current_allocation
    except (httpx.RequestError, ValueError, KeyError) as exc:
        log.error("Failed to fetch risk score: %s", exc)
        return current_allocation

    score = float(risk["volatility_score"])
    log.info(
        "Fetched risk — score=%.2f horizon=%s risk_level=%s",
        score,
        risk.get("horizon"),
        risk.get("risk_level"),
    )

    # --- 2. Decide ---
    should_rebalance, target_allocation = needs_rebalance(risk, current_allocation)

    status = "skipped"
    tx_hash = None
    error_message = None

    if should_rebalance:
        try:
            # --- 3. Build ---
            transaction = build_rebalance_transaction(target_allocation, score)

            # --- 4. Sign ---
            signature = sign_transaction(transaction)

            # --- 5. Submit ---
            rpc_response = submit_to_soroban(transaction, signature)
            tx_hash = rpc_response.get("result", {}).get("hash")
            status = "submitted"
            log.info(
                "Rebalance submitted — allocation=%s tx_hash=%s",
                target_allocation,
                tx_hash,
            )
            current_allocation = target_allocation

        except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError, OSError) as exc:
            error_message = str(exc)
            status = "failed"
            log.error("Rebalance failed: %s", exc)
    else:
        log.info("No rebalance needed — allocation remains '%s'", current_allocation)

    # --- 6. Persist event ---
    try:
        save_rebalance_event(
            timestamp=datetime.now(timezone.utc),
            volatility_score=score,
            threshold=REBALANCE_THRESHOLD,
            previous_allocation=current_allocation if status != "submitted" else (
                "risky" if target_allocation == "stable" else "stable"
            ),
            target_allocation=target_allocation,
            status=status,
            tx_hash=tx_hash,
            error_message=error_message,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("Could not persist rebalance event: %s", exc)

    log.info("=== Rebalance cycle complete — status=%s ===", status)
    return current_allocation


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    log.info(
        "Keeper Bot starting — threshold=%.1f poll_interval=%ds signer=%s",
        REBALANCE_THRESHOLD,
        POLL_INTERVAL_SECONDS,
        SIGNER_BACKEND,
    )

    # Default starting allocation — could be bootstrapped from on-chain state
    current_allocation = os.getenv("INITIAL_ALLOCATION", "risky")
    log.info("Initial allocation: %s", current_allocation)

    while True:
        current_allocation = run_rebalance_cycle(current_allocation)
        log.info("Sleeping for %d seconds until next poll...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
