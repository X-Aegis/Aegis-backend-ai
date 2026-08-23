"""
Keeper Bot — Off-chain worker (BK-5)

Polls the Model API every hour. When the volatility-derived allocation
recommendation changes by more than REBALANCE_THRESHOLD, it:
  1. Builds a `rebalance` transaction payload.
  2. Signs it via AWS KMS or HashiCorp Vault (configurable).
  3. Submits the signed transaction to the Soroban RPC endpoint.
"""

import asyncio
import logging
import os
import sys

import httpx
from dotenv import load_dotenv
from stellar_sdk import Keypair, Server, TransactionBuilder, scval

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

# Local Wallet signing key — standard Stellar Secret Key (S...)
ADMIN_SECRET_KEY: str = os.getenv("ADMIN_SECRET_KEY", "")

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
# Transaction building & Submission (Stellar SDK)
# ---------------------------------------------------------------------------

import boto3
import hvac


def execute_rebalance_transaction(
    target_stable_pct: float,
    volatility_score: float,
    contract_id: str,
    source_secret: str,
) -> dict:
    """
    Constructs, simulates, signs, and submits a real Soroban transaction
    using the official stellar-sdk.
    """
    import base64
    from lib.database import record_audit_log, get_keeper_key_status
    import stellar_sdk

    if SIGNING_BACKEND in ["aws_kms", "vault"]:
        # When using enterprise HSMs, source_secret should actually be the public key (SOROBAN_SOURCE_ACCOUNT)
        # to construct the transaction footprint without needing the plaintext private key.
        account_pubkey = SOROBAN_SOURCE_ACCOUNT
        if not account_pubkey:
            raise OSError("SOROBAN_SOURCE_ACCOUNT is not set. Cannot build transaction.")
        keypair = Keypair.from_public_key(account_pubkey)
    else:
        if not source_secret:
            raise OSError("ADMIN_SECRET_KEY is not set. Cannot sign transaction.")
        keypair = Keypair.from_secret(source_secret)
        account_pubkey = keypair.public_key

    server = Server(SOROBAN_RPC_URL)
    
    log.info("Loading account details for %s...", account_pubkey)
    source_account = server.load_account(account_pubkey)
    
    # 1. Build the base transaction
    tx = (
        TransactionBuilder(
            source_account=source_account,
            network_passphrase=SOROBAN_NETWORK_PASSPHRASE,
            base_fee=1000,
        )
        .append_invoke_contract_function_op(
            contract_id=contract_id,
            function_name="rebalance",
            parameters=[scval.to_address(account_pubkey)]
        )
        .set_timeout(30)
        .build()
    )
    
    # 2. Simulate transaction to get footprint and resource fees
    log.info("Simulating transaction to fetch Soroban footprint...")
    sim_result = server.simulate_transaction(tx)
    
    if hasattr(sim_result, "error") and sim_result.error:
        raise RuntimeError(f"Simulation failed: {sim_result.error}")
        
    log.info("Simulation successful. Applying footprint and fees.")
    tx.soroban_data = sim_result.transactionData
    
    if hasattr(sim_result, "minResourceFee"):
        tx.add_resource_fee(int(sim_result.minResourceFee))
        
    # 3. Sign the transaction
    tx_hash = tx.hash()
    
    try:
        if SIGNING_BACKEND == "aws_kms":
            # Check key status from DB
            key_status = get_keeper_key_status(AWS_KMS_KEY_ID)
            if key_status == "revoked":
                raise RuntimeError(f"Key {AWS_KMS_KEY_ID} is revoked. Halting signing.")

            log.info(f"Signing via AWS KMS using key {AWS_KMS_KEY_ID}...")
            client = boto3.client('kms', region_name=AWS_REGION)
            response = client.sign(
                KeyId=AWS_KMS_KEY_ID,
                Message=tx_hash,
                MessageType='RAW',
                SigningAlgorithm='RSASSA_PKCS1_V1_5_SHA_256'
            )
            raw_signature = response['Signature']
            
            hint = stellar_sdk.xdr.SignatureHint(keypair.signature_hint())
            sig = stellar_sdk.xdr.Signature(raw_signature)
            dec_sig = stellar_sdk.xdr.DecoratedSignature(hint=hint, signature=sig)
            tx.signatures.append(dec_sig)
            
            record_audit_log(tx_hash.hex(), SIGNING_BACKEND, AWS_KMS_KEY_ID, "SUCCESS")

        elif SIGNING_BACKEND == "vault":
            # Check key status from DB
            key_status = get_keeper_key_status(VAULT_KEY_PATH)
            if key_status == "revoked":
                raise RuntimeError(f"Key {VAULT_KEY_PATH} is revoked. Halting signing.")

            log.info(f"Signing via HashiCorp Vault using path {VAULT_KEY_PATH}...")
            client = hvac.Client(url=VAULT_ADDR, token=VAULT_TOKEN)
            response = client.secrets.transit.sign_data(
                name='admin-key', # using the transit key name directly
                hash_input=base64.b64encode(tx_hash).decode('utf-8'),
                hash_algorithm='none'
            )
            
            vault_sig = response['data']['signature']
            # Vault signature format: vault:v1:<base64-encoded-signature>
            if vault_sig.startswith("vault:v1:"):
                vault_sig = vault_sig[len("vault:v1:"):]
            
            raw_signature = base64.b64decode(vault_sig)
            
            hint = stellar_sdk.xdr.SignatureHint(keypair.signature_hint())
            sig = stellar_sdk.xdr.Signature(raw_signature)
            dec_sig = stellar_sdk.xdr.DecoratedSignature(hint=hint, signature=sig)
            tx.signatures.append(dec_sig)
            
            record_audit_log(tx_hash.hex(), SIGNING_BACKEND, VAULT_KEY_PATH, "SUCCESS")

        else:
            log.info("Signing via local wallet (env_key)...")
            tx.sign(keypair)
            
    except Exception as e:
        key_identifier = AWS_KMS_KEY_ID if SIGNING_BACKEND == "aws_kms" else (VAULT_KEY_PATH if SIGNING_BACKEND == "vault" else "env_key")
        record_audit_log(tx_hash.hex(), SIGNING_BACKEND, key_identifier, "FAILED", str(e))
        raise RuntimeError(f"Signing failed: {e}")
    
    # 4. Submit to network
    log.info("Submitting transaction to network...")
    send_response = server.send_transaction(tx)
    
    if send_response.get("errorResultXdr"):
        raise RuntimeError(f"Transaction submission failed: {send_response['errorResultXdr']}")
        
    log.info("Transaction submitted! Hash: %s", send_response.get("hash"))
    return send_response

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

        # 4. Guard: require contract config
        if not SOROBAN_CONTRACT_ID or not ADMIN_SECRET_KEY:
            log.error(
                "SOROBAN_CONTRACT_ID or ADMIN_SECRET_KEY is not configured. "
                "Skipping submission."
            )
            return

        # 5. Build, sign, and submit via Stellar SDK
        try:
            # We run this synchronous stellar-sdk process in the asyncio loop thread
            # since this bot only polls once an hour and won't block high-traffic endpoints.
            execute_rebalance_transaction(
                target_stable_pct=target,
                volatility_score=score,
                contract_id=SOROBAN_CONTRACT_ID,
                source_secret=ADMIN_SECRET_KEY,
            )
            self._last_allocation = target
            log.info("Rebalance submitted successfully. New allocation: %.1f %% stable.", target)
        except Exception as exc:  # noqa: BLE001 - keep the hourly loop alive on any SDK error
            log.error("Soroban transaction failed: %s", exc)

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
