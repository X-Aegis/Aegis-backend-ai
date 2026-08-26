"""
Keeper Bot — Off-chain worker (BK-5)

Polls the Model API every hour. When the volatility-derived allocation
recommendation changes by more than REBALANCE_THRESHOLD, it:
  1. Builds a `rebalance` transaction payload.
  2. Signs it via AWS KMS or HashiCorp Vault (configurable).
  3. Submits the signed transaction to the Soroban RPC endpoint.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Add project root to sys.path for local imports
# ---------------------------------------------------------------------------
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration (all values come from environment variables)
# ---------------------------------------------------------------------------

# Model API
MODEL_API_BASE_URL: str = os.getenv("MODEL_API_BASE_URL", "http://localhost:8000")
RISK_HORIZON: int = int(os.getenv("RISK_HORIZON", "1"))

# Rebalancing policy
REBALANCE_THRESHOLD: float = float(os.getenv("REBALANCE_THRESHOLD", "5.0"))
"""Minimum change in recommended allocation (%) required to trigger rebalance."""

HIGH_VOLATILITY_CUTOFF: float = float(os.getenv("HIGH_VOLATILITY_CUTOFF", "80.0"))
"""Volatility score above which the vault should be 100 % in stable assets."""

POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "3600"))  # 1 hour

# Soroban RPC
SOROBAN_RPC_URL: str = os.getenv("SOROBAN_RPC_URL", "https://soroban-testnet.stellar.org")
SOROBAN_CONTRACT_ID: str = os.getenv("SOROBAN_CONTRACT_ID", "")
SOROBAN_SOURCE_ACCOUNT: str = os.getenv("SOROBAN_SOURCE_ACCOUNT", "")
SOROBAN_NETWORK_PASSPHRASE: str = os.getenv(
    "SOROBAN_NETWORK_PASSPHRASE", "Test SDF Network ; September 2015"
)

# Signing backend: "aws_kms" | "vault" | "env_key"
SIGNING_BACKEND: str = os.getenv("SIGNING_BACKEND", "env_key")

# AWS KMS (used when SIGNING_BACKEND == "aws_kms")
AWS_KMS_KEY_ID: str = os.getenv("AWS_KMS_KEY_ID", "")
AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

# HashiCorp Vault (used when SIGNING_BACKEND == "vault")
VAULT_ADDR: str = os.getenv("VAULT_ADDR", "http://127.0.0.1:8200")
VAULT_TOKEN: str = os.getenv("VAULT_TOKEN", "")
VAULT_KEY_PATH: str = os.getenv("VAULT_KEY_PATH", "transit/sign/admin-key")

# Fallback signing key — hex-encoded 32-byte secret (dev/test only)
ADMIN_SECRET_KEY_HEX: str = os.getenv("ADMIN_SECRET_KEY_HEX", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] keeper_bot — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("keeper_bot")


# ---------------------------------------------------------------------------
# Allocation logic
# ---------------------------------------------------------------------------

def compute_target_allocation(volatility_score: float) -> float:
    """
    Returns the target percentage of the vault that should be in *stable* assets
    given the current volatility score (0–100).

    Simple two-regime model that mirrors the backtest engine logic:
      - score >= HIGH_VOLATILITY_CUTOFF → 100 % stable
      - score <  HIGH_VOLATILITY_CUTOFF → 0 % stable  (fully in risky FX asset)

    A more granular linear interpolation could be substituted here later.
    """
    return 100.0 if volatility_score >= HIGH_VOLATILITY_CUTOFF else 0.0


# ---------------------------------------------------------------------------
# Transaction building
# ---------------------------------------------------------------------------

def build_rebalance_transaction(
    target_stable_pct: float,
    volatility_score: float,
    contract_id: str,
    source_account: str,
) -> dict:
    """
    Constructs the rebalance transaction payload that will be sent to the
    Soroban RPC.

    In a full production deployment this would use the Stellar Python SDK to
    produce a proper XDR-encoded Soroban `invokeHostFunction` transaction.
    Here we build a structured dict that represents the intent; the XDR
    serialization step is handled in `_encode_transaction_xdr`.
    """
    return {
        "contract_id": contract_id,
        "function": "rebalance",
        "args": {
            "target_stable_pct": int(target_stable_pct),
            "volatility_score": round(volatility_score, 4),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "source_account": source_account,
        "network_passphrase": SOROBAN_NETWORK_PASSPHRASE,
    }


def _encode_transaction_xdr(tx_payload: dict) -> bytes:
    """
    Encodes the transaction payload as canonical bytes ready for signing.

    Production note: replace this with actual XDR serialisation using the
    `stellar-sdk` library (`stellar_sdk.TransactionEnvelope`).
    """
    canonical = json.dumps(tx_payload, sort_keys=True, separators=(",", ":"))
    return canonical.encode("utf-8")


# ---------------------------------------------------------------------------
# Signing backends
# ---------------------------------------------------------------------------

async def sign_with_aws_kms(tx_bytes: bytes) -> str:
    """
    Signs tx_bytes using AWS KMS (asymmetric SIGN_VERIFY key).
    Returns the base-64 encoded DER signature string.
    Requires: boto3  (`pip install boto3`)
    """
    try:
        import base64

        import boto3  # type: ignore

        client = boto3.client("kms", region_name=AWS_REGION)
        digest = hashlib.sha256(tx_bytes).digest()

        response = client.sign(
            KeyId=AWS_KMS_KEY_ID,
            Message=digest,
            MessageType="DIGEST",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        signature_b64 = base64.b64encode(response["Signature"]).decode()
        log.info("Transaction signed via AWS KMS (key: %s)", AWS_KMS_KEY_ID)
        return signature_b64

    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        log.error("AWS KMS signing failed: %s", exc)
        raise


async def sign_with_vault(tx_bytes: bytes) -> str:
    """
    Signs tx_bytes using HashiCorp Vault Transit secrets engine.
    Returns the Vault-provided signature string.
    """
    try:
        import base64

        encoded = base64.b64encode(tx_bytes).decode()
        url = f"{VAULT_ADDR}/v1/{VAULT_KEY_PATH}"
        headers = {"X-Vault-Token": VAULT_TOKEN}
        payload = {"input": encoded}

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers, timeout=10)
            response.raise_for_status()
            data = response.json()
            signature = data["data"]["signature"]
            log.info("Transaction signed via HashiCorp Vault (path: %s)", VAULT_KEY_PATH)
            return signature

    except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError) as exc:
        log.error("HashiCorp Vault signing failed: %s", exc)
        raise


async def sign_with_env_key(tx_bytes: bytes) -> str:
    """
    Signs tx_bytes using an HMAC-SHA256 derived from the env secret key.
    For development/testing only — not suitable for production.
    """
    import base64
    import hmac

    if not ADMIN_SECRET_KEY_HEX:
        raise OSError(
            "ADMIN_SECRET_KEY_HEX is not set. "
            "Provide a signing key or configure AWS KMS / HashiCorp Vault."
        )

    secret = bytes.fromhex(ADMIN_SECRET_KEY_HEX)
    sig = hmac.new(secret, tx_bytes, hashlib.sha256).digest()
    signature = base64.b64encode(sig).decode()
    log.warning("Transaction signed with env key — use KMS or Vault in production.")
    return signature


async def sign_transaction(tx_bytes: bytes) -> str:
    """Dispatches to the configured signing backend."""
    if SIGNING_BACKEND == "aws_kms":
        return await sign_with_aws_kms(tx_bytes)
    if SIGNING_BACKEND == "vault":
        return await sign_with_vault(tx_bytes)
    # Default / fallback
    return await sign_with_env_key(tx_bytes)


# ---------------------------------------------------------------------------
# Soroban RPC submission
# ---------------------------------------------------------------------------

async def submit_to_soroban(tx_payload: dict, signature: str) -> dict:
    """
    Sends the signed transaction to the Soroban RPC endpoint using the
    `sendTransaction` JSON-RPC method.

    Production note: wrap this with proper XDR + stellar-sdk `Server.send_transaction`.
    """
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "sendTransaction",
        "params": {
            "transaction": json.dumps(tx_payload),
            "signature": signature,
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(SOROBAN_RPC_URL, json=envelope)
        response.raise_for_status()
        result = response.json()

    if "error" in result:
        raise RuntimeError(f"Soroban RPC error: {result['error']}")

    log.info("Soroban submission result: %s", result.get("result"))
    return result


# ---------------------------------------------------------------------------
# Model API polling
# ---------------------------------------------------------------------------

async def fetch_volatility_score() -> float:
    """Fetches the latest volatility score from the Model API."""
    url = f"{MODEL_API_BASE_URL}/risk/current"
    params = {"horizon": RISK_HORIZON}

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    score = float(data["volatility_score"])
    log.info(
        "Fetched volatility score: %.2f (risk_level=%s, horizon=%dh)",
        score,
        data.get("risk_level", "?"),
        RISK_HORIZON,
    )
    return score


# ---------------------------------------------------------------------------
# Core keeper loop
# ---------------------------------------------------------------------------

class KeeperBot:
    """Stateful keeper that remembers the last submitted allocation."""

    def __init__(self) -> None:
        self._last_allocation: float | None = None

    async def run_once(self) -> None:
        """Single poll-and-rebalance cycle."""
        # 1. Fetch current volatility score
        try:
            score = await fetch_volatility_score()
        except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError) as exc:
            log.error("Failed to fetch volatility score: %s", exc)
            return

        # 2. Compute recommended allocation
        target = compute_target_allocation(score)
        log.info("Target allocation: %.1f %% stable", target)

        # 3. Skip if change is below threshold
        if self._last_allocation is not None:
            delta = abs(target - self._last_allocation)
            if delta < REBALANCE_THRESHOLD:
                log.info(
                    "Allocation delta %.2f %% < threshold %.2f %% — no rebalance needed.",
                    delta,
                    REBALANCE_THRESHOLD,
                )
                return
            log.info(
                "Allocation change %.2f %% >= threshold %.2f %% — triggering rebalance.",
                delta,
                REBALANCE_THRESHOLD,
            )
        else:
            log.info("First run — submitting initial allocation.")

        # 4. Guard: require contract and account config
        if not SOROBAN_CONTRACT_ID or not SOROBAN_SOURCE_ACCOUNT:
            log.error(
                "SOROBAN_CONTRACT_ID or SOROBAN_SOURCE_ACCOUNT is not configured. "
                "Skipping submission."
            )
            return

        # 5. Build transaction
        tx_payload = build_rebalance_transaction(
            target_stable_pct=target,
            volatility_score=score,
            contract_id=SOROBAN_CONTRACT_ID,
            source_account=SOROBAN_SOURCE_ACCOUNT,
        )
        log.info("Built rebalance transaction: %s", json.dumps(tx_payload))

        # 6. Sign
        try:
            tx_bytes = _encode_transaction_xdr(tx_payload)
            signature = await sign_transaction(tx_bytes)
        except (RuntimeError, OSError, ValueError) as exc:
            log.error("Signing failed — aborting submission: %s", exc)
            return

        # 7. Submit
        try:
            await submit_to_soroban(tx_payload, signature)
            self._last_allocation = target
            log.info("Rebalance submitted successfully. New allocation: %.1f %% stable.", target)
        except (httpx.RequestError, httpx.HTTPStatusError, RuntimeError) as exc:
            log.error("Soroban submission failed: %s", exc)

    async def run(self) -> None:
        """Main loop: poll every POLL_INTERVAL_SECONDS."""
        log.info(
            "Keeper bot starting — poll interval: %ds, rebalance threshold: %.1f %%",
            POLL_INTERVAL_SECONDS,
            REBALANCE_THRESHOLD,
        )
        while True:
            await self.run_once()
            log.info("Sleeping %d seconds until next poll…", POLL_INTERVAL_SECONDS)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    asyncio.run(KeeperBot().run())
