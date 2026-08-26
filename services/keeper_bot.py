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
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from stellar_sdk import Keypair, Server, TransactionBuilder, scval

# ---------------------------------------------------------------------------
# Add project root to sys.path for local imports
# ---------------------------------------------------------------------------
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.database import (
    get_keeper_stats,
    get_keeper_status,
    get_last_submitted_allocation,
    get_manual_override,
    record_keeper_decision,
    record_keeper_failure,
    record_keeper_heartbeat,
)

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

MAX_REBALANCES_PER_24H = 4
MAX_ALLOCATION_CHANGE_PCT = 20.0

# Soroban RPC
SOROBAN_RPC_URL: str = os.getenv(
    "SOROBAN_RPC_URL", "https://soroban-testnet.stellar.org"
)
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
    if SIGNING_BACKEND in ["aws_kms", "vault"]:
        # TODO: Implement XDR parsing and raw signature injection for enterprise HSMs.
        log.warning(
            "Enterprise signing (KMS/Vault) selected but not yet implemented for Soroban XDR. Falling back to local wallet if available."
        )

    if not source_secret:
        raise OSError("ADMIN_SECRET_KEY is not set. Cannot sign transaction.")

    server = Server(SOROBAN_RPC_URL)
    keypair = Keypair.from_secret(source_secret)

    log.info("Loading account details for %s...", keypair.public_key)
    source_account = server.load_account(keypair.public_key)

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
            parameters=[scval.to_address(keypair.public_key)],
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
    tx.sign(keypair)

    # 4. Submit to network
    log.info("Submitting transaction to network...")
    send_response = server.send_transaction(tx)

    if send_response.get("errorResultXdr"):
        raise RuntimeError(
            f"Transaction submission failed: {send_response['errorResultXdr']}"
        )

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

    def __init__(self, now_fn=None) -> None:
        self._last_allocation: float | None = None
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def _policy_checks(
        self, target: float, current_allocation: float | None, manual_override: bool
    ) -> dict:
        stats = get_keeper_stats()
        rebalance_count = int(stats["count_last_24h"])
        allocation_change = (
            abs(target - current_allocation) if current_allocation is not None else 0.0
        )
        return {
            "manual_override": manual_override,
            "rate_limit": {
                "passed": manual_override or rebalance_count < MAX_REBALANCES_PER_24H,
                "count_last_24h": rebalance_count,
                "max_rebalances_per_24h": MAX_REBALANCES_PER_24H,
            },
            "allocation_change": {
                "passed": manual_override
                or allocation_change <= MAX_ALLOCATION_CHANGE_PCT,
                "change_percentage_points": allocation_change,
                "max_percentage_points": MAX_ALLOCATION_CHANGE_PCT,
            },
        }

    @staticmethod
    def _is_policy_approved(checks: dict) -> bool:
        return checks["rate_limit"]["passed"] and checks["allocation_change"]["passed"]

    @staticmethod
    def _proposed_allocations(target: float) -> dict:
        return {"stable": target, "risky": 100.0 - target}

    async def run_once(self) -> None:
        """Single poll-and-rebalance cycle."""
        # 0. Check circuit breaker and dead-man switch
        status = get_keeper_status()
        if status:
            failures = status.get("consecutive_failures", 0)
            last_heartbeat = status.get("last_heartbeat")
            if last_heartbeat:
                now = self._now()
                diff_hours = (now - last_heartbeat).total_seconds() / 3600
                if diff_hours >= 6:
                    log.error(
                        "DEAD-MAN SWITCH ACTIVATED (no heartbeat in >= 6 hours). Halting keeper operations."
                    )
                    return

            if failures >= 3:
                log.error(
                    "Circuit breaker TRIPPED (>= 3 failures). Halting keeper operations."
                )
                return

        # 1. Fetch current volatility score
        try:
            score = await fetch_volatility_score()
        except (httpx.RequestError, httpx.HTTPStatusError, KeyError, ValueError) as exc:
            log.error("Failed to fetch volatility score: %s", exc)
            return

        # 2. Compute recommended allocation
        target = compute_target_allocation(score)
        log.info("Target allocation: %.1f %% stable", target)

        proposed_allocations = self._proposed_allocations(target)
        current_allocation = self._last_allocation
        if current_allocation is None:
            current_allocation = get_last_submitted_allocation()
        manual_override = get_manual_override()
        checks = self._policy_checks(target, current_allocation, manual_override)
        if not self._is_policy_approved(checks):
            record_keeper_decision(score, proposed_allocations, checks, "rejected")
            log.warning("Rebalance rejected by keeper policy: %s", checks)
            return

        # 3. Skip if change is below threshold
        if current_allocation is not None:
            delta = abs(target - current_allocation)
            if delta < REBALANCE_THRESHOLD:
                checks["rebalance_threshold"] = {
                    "passed": False,
                    "change_percentage_points": delta,
                    "min_percentage_points": REBALANCE_THRESHOLD,
                }
                record_keeper_decision(score, proposed_allocations, checks, "rejected")
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

        checks["rebalance_threshold"] = {
            "passed": True,
            "change_percentage_points": (
                abs(target - current_allocation)
                if current_allocation is not None
                else None
            ),
            "min_percentage_points": REBALANCE_THRESHOLD,
        }

        # 4. Guard: require contract config
        if not SOROBAN_CONTRACT_ID or not ADMIN_SECRET_KEY:
            record_keeper_decision(score, proposed_allocations, checks, "rejected")
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
            checks["submission"] = {"passed": True}
            record_keeper_decision(
                score,
                proposed_allocations,
                checks,
                "approved",
                transaction_submitted=True,
            )
            log.info(
                "Rebalance submitted successfully. New allocation: %.1f %% stable.",
                target,
            )
            record_keeper_heartbeat()
        except Exception as exc:  # noqa: BLE001 - keep the hourly loop alive on any SDK error
            log.error("Soroban transaction failed: %s", exc)
            checks["submission"] = {"passed": False, "error": str(exc)}
            record_keeper_decision(score, proposed_allocations, checks, "rejected")
            record_keeper_failure()

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
