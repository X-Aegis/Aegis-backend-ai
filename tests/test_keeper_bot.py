"""
Tests for services/keeper_bot.py
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import services.keeper_bot as kb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# compute_target_allocation
# ---------------------------------------------------------------------------

class TestComputeTargetAllocation:
    def test_high_volatility_returns_100_stable(self):
        assert kb.compute_target_allocation(80.0) == 100.0

    def test_above_cutoff_returns_100_stable(self):
        assert kb.compute_target_allocation(95.0) == 100.0

    def test_below_cutoff_returns_0_stable(self):
        assert kb.compute_target_allocation(79.9) == 0.0

    def test_zero_score_returns_0_stable(self):
        assert kb.compute_target_allocation(0.0) == 0.0


# ---------------------------------------------------------------------------
# build_rebalance_transaction
# ---------------------------------------------------------------------------

class TestBuildRebalanceTransaction:
    def test_structure(self):
        tx = kb.build_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_account="GABC",
        )
        assert tx["function"] == "rebalance"
        assert tx["contract_id"] == "CONTRACT_XYZ"
        assert tx["args"]["target_stable_pct"] == 100
        assert tx["args"]["volatility_score"] == 85.5
        assert "timestamp" in tx["args"]

    def test_zero_allocation(self):
        tx = kb.build_rebalance_transaction(
            target_stable_pct=0.0,
            volatility_score=30.0,
            contract_id="C",
            source_account="S",
        )
        assert tx["args"]["target_stable_pct"] == 0


# ---------------------------------------------------------------------------
# _encode_transaction_xdr
# ---------------------------------------------------------------------------

class TestEncodeTransactionXdr:
    def test_returns_bytes(self):
        tx = {"a": 1, "b": 2}
        result = kb._encode_transaction_xdr(tx)
        assert isinstance(result, bytes)

    def test_deterministic(self):
        tx = {"z": 9, "a": 1}
        assert kb._encode_transaction_xdr(tx) == kb._encode_transaction_xdr(tx)

    def test_different_payloads_differ(self):
        assert kb._encode_transaction_xdr({"x": 1}) != kb._encode_transaction_xdr({"x": 2})


# ---------------------------------------------------------------------------
# sign_with_env_key
# ---------------------------------------------------------------------------

class TestSignWithEnvKey:
    def test_signs_successfully(self, monkeypatch):
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "a" * 64)
        sig = _run(kb.sign_with_env_key(b"payload"))
        assert isinstance(sig, str)
        assert len(sig) > 0

    def test_raises_without_key(self, monkeypatch):
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "")
        with pytest.raises(EnvironmentError):
            _run(kb.sign_with_env_key(b"payload"))

    def test_different_payloads_produce_different_sigs(self, monkeypatch):
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "b" * 64)
        sig1 = _run(kb.sign_with_env_key(b"payload_one"))
        sig2 = _run(kb.sign_with_env_key(b"payload_two"))
        assert sig1 != sig2


# ---------------------------------------------------------------------------
# sign_transaction dispatch
# ---------------------------------------------------------------------------

class TestSignTransaction:
    def test_dispatches_to_env_key(self, monkeypatch):
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "c" * 64)
        sig = _run(kb.sign_transaction(b"data"))
        assert isinstance(sig, str)

    def test_dispatches_to_aws_kms(self, monkeypatch):
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "aws_kms")
        monkeypatch.setattr(kb, "AWS_KMS_KEY_ID", "fake-key-id")
        mock_sign = AsyncMock(return_value="kms-sig")
        monkeypatch.setattr(kb, "sign_with_aws_kms", mock_sign)
        sig = _run(kb.sign_transaction(b"data"))
        assert sig == "kms-sig"
        mock_sign.assert_awaited_once_with(b"data")

    def test_dispatches_to_vault(self, monkeypatch):
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "vault")
        mock_sign = AsyncMock(return_value="vault-sig")
        monkeypatch.setattr(kb, "sign_with_vault", mock_sign)
        sig = _run(kb.sign_transaction(b"data"))
        assert sig == "vault-sig"
        mock_sign.assert_awaited_once_with(b"data")


# ---------------------------------------------------------------------------
# KeeperBot.run_once
# ---------------------------------------------------------------------------

class TestKeeperBotRunOnce:
    def _bot(self):
        return kb.KeeperBot()

    def test_skips_when_delta_below_threshold(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 100.0  # already fully stable

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", "S")

        # Score still high → target stays 100 % stable → delta == 0
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=90.0))
        mock_submit = AsyncMock()
        monkeypatch.setattr(kb, "submit_to_soroban", mock_submit)

        _run(bot.run_once())
        mock_submit.assert_not_awaited()

    def test_triggers_rebalance_when_delta_above_threshold(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0  # currently in risky

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", "S")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "d" * 64)
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")

        # Score flips above cutoff → target becomes 100 % stable → delta == 100
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_submit = AsyncMock(return_value={"result": {"status": "PENDING"}})
        monkeypatch.setattr(kb, "submit_to_soroban", mock_submit)

        _run(bot.run_once())
        mock_submit.assert_awaited_once()
        assert bot._last_allocation == 100.0

    def test_skips_submission_when_contract_id_missing(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", "S")
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_submit = AsyncMock()
        monkeypatch.setattr(kb, "submit_to_soroban", mock_submit)

        _run(bot.run_once())
        mock_submit.assert_not_awaited()

    def test_handles_api_fetch_failure_gracefully(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(
            kb, "fetch_volatility_score",
            AsyncMock(side_effect=httpx.RequestError("timeout")),
        )
        mock_submit = AsyncMock()
        monkeypatch.setattr(kb, "submit_to_soroban", mock_submit)

        # Should not raise
        _run(bot.run_once())
        mock_submit.assert_not_awaited()

    def test_handles_signing_failure_gracefully(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", "S")
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        monkeypatch.setattr(
            kb, "sign_transaction", AsyncMock(side_effect=RuntimeError("KMS error"))
        )
        mock_submit = AsyncMock()
        monkeypatch.setattr(kb, "submit_to_soroban", mock_submit)

        _run(bot.run_once())
        mock_submit.assert_not_awaited()

    def test_does_not_update_allocation_on_submission_failure(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", "S")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY_HEX", "e" * 64)
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        monkeypatch.setattr(
            kb, "submit_to_soroban", AsyncMock(side_effect=RuntimeError("RPC down"))
        )

        _run(bot.run_once())
        assert bot._last_allocation == 0.0  # unchanged after failed submission
