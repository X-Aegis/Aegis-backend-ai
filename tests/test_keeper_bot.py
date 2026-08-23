"""
Tests for services/keeper_bot.py
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stellar_sdk import Keypair as StellarKeypair

import services.keeper_bot as kb

TEST_KEYPAIR = StellarKeypair.random()
TEST_PUBLIC_KEY = TEST_KEYPAIR.public_key
TEST_SECRET_KEY = TEST_KEYPAIR.secret


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
# execute_rebalance_transaction
# ---------------------------------------------------------------------------

class TestExecuteRebalanceTransaction:
    def test_raises_without_secret(self, monkeypatch):
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        with pytest.raises(OSError, match="ADMIN_SECRET_KEY"):
            kb.execute_rebalance_transaction(
                target_stable_pct=100.0,
                volatility_score=85.5,
                contract_id="CONTRACT_XYZ",
                source_secret="",
            )

    def test_raises_with_invalid_secret(self, monkeypatch):
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        with pytest.raises(ValueError):
            kb.execute_rebalance_transaction(
                target_stable_pct=100.0,
                volatility_score=85.5,
                contract_id="CONTRACT_XYZ",
                source_secret="not-a-valid-secret",
            )

    def test_builds_and_submits_transaction(self, monkeypatch):
        # Mock the server + keypair pipeline
        fake_keypair = Mock()
        fake_keypair.public_key = TEST_PUBLIC_KEY
        fake_source = Mock()

        fake_tx = Mock()
        fake_sim = Mock()
        fake_sim.error = None
        fake_sim.transactionData = "footprint-data"
        fake_sim.minResourceFee = 1000

        fake_send = {"hash": "abc123", "status": "PENDING"}

        fake_server = Mock()
        fake_server.load_account.return_value = fake_source
        fake_server.simulate_transaction.return_value = fake_sim
        fake_server.send_transaction.return_value = fake_send

        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb, "Keypair", Mock(from_secret=lambda s: fake_keypair))
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())

        # Make TransactionBuilder chain work
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        result = kb.execute_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_secret=TEST_SECRET_KEY,
        )

        fake_server.load_account.assert_called_once_with(TEST_PUBLIC_KEY)
        fake_server.simulate_transaction.assert_called_once_with(fake_tx)
        fake_server.send_transaction.assert_called_once_with(fake_tx)
        fake_tx.soroban_data = "footprint-data"
        assert fake_tx.soroban_data == "footprint-data"
        assert result == fake_send

    def test_builds_and_submits_transaction_kms(self, monkeypatch):
        fake_tx = Mock()
        fake_tx.hash.return_value = b'testhash'
        fake_tx.signatures = []
        
        fake_sim = Mock()
        fake_sim.error = None
        fake_sim.transactionData = "footprint-data"
        fake_sim.minResourceFee = 1000

        fake_send = {"hash": "abc123kms"}

        fake_server = Mock()
        fake_server.load_account.return_value = Mock()
        fake_server.simulate_transaction.return_value = fake_sim
        fake_server.send_transaction.return_value = fake_send

        fake_kms_client = Mock()
        fake_kms_client.sign.return_value = {'Signature': b'fakesig'}
        
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "aws_kms")
        monkeypatch.setattr(kb, "AWS_KMS_KEY_ID", "kms-key")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", TEST_PUBLIC_KEY)
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb.boto3, "client", lambda service, region_name: fake_kms_client)
        monkeypatch.setattr(kb, "get_keeper_key_status", lambda k: "active")
        monkeypatch.setattr(kb, "record_audit_log", Mock())
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())
        
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        result = kb.execute_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_secret="",
        )
        assert len(fake_tx.signatures) == 1
        assert result == fake_send
        kb.record_audit_log.assert_called_once_with('7465737468617368', 'aws_kms', 'kms-key', 'SUCCESS')

    def test_builds_and_submits_transaction_vault(self, monkeypatch):
        fake_tx = Mock()
        fake_tx.hash.return_value = b'testhash'
        fake_tx.signatures = []
        
        fake_sim = Mock()
        fake_sim.error = None
        fake_sim.transactionData = "footprint-data"
        fake_sim.minResourceFee = 1000

        fake_send = {"hash": "abc123vault"}

        fake_server = Mock()
        fake_server.load_account.return_value = Mock()
        fake_server.simulate_transaction.return_value = fake_sim
        fake_server.send_transaction.return_value = fake_send

        fake_vault_client = Mock()
        fake_vault_client.secrets.transit.sign_data.return_value = {'data': {'signature': 'vault:v1:ZmFrZXNpZw=='}} # 'fakesig' base64 encoded
        
        monkeypatch.setattr(kb, "SIGNING_BACKEND", "vault")
        monkeypatch.setattr(kb, "VAULT_KEY_PATH", "vault-key")
        monkeypatch.setattr(kb, "SOROBAN_SOURCE_ACCOUNT", TEST_PUBLIC_KEY)
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb.hvac, "Client", lambda url, token: fake_vault_client)
        monkeypatch.setattr(kb, "get_keeper_key_status", lambda k: "active")
        monkeypatch.setattr(kb, "record_audit_log", Mock())
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())
        
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        result = kb.execute_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_secret="",
        )
        assert len(fake_tx.signatures) == 1
        assert result == fake_send
        kb.record_audit_log.assert_called_once_with('7465737468617368', 'vault', 'vault-key', 'SUCCESS')

    def test_raises_when_simulation_fails(self, monkeypatch):
        fake_keypair = Mock()
        fake_keypair.public_key = TEST_PUBLIC_KEY
        fake_tx = Mock()
        fake_sim = Mock()
        fake_sim.error = "Simulation failed: bad footprint"
        fake_sim.transactionData = None

        fake_server = Mock()
        fake_server.load_account.return_value = Mock()
        fake_server.simulate_transaction.return_value = fake_sim

        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb, "Keypair", Mock(from_secret=lambda s: fake_keypair))
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        with pytest.raises(RuntimeError, match="Simulation failed"):
            kb.execute_rebalance_transaction(
                target_stable_pct=100.0,
                volatility_score=85.5,
                contract_id="CONTRACT_XYZ",
                source_secret=TEST_SECRET_KEY,
            )

    def test_raises_when_submission_has_error_result(self, monkeypatch):
        fake_keypair = Mock()
        fake_keypair.public_key = TEST_PUBLIC_KEY
        fake_tx = Mock()
        fake_sim = Mock()
        fake_sim.error = None
        fake_sim.transactionData = "footprint-data"
        fake_sim.minResourceFee = 1000

        fake_server = Mock()
        fake_server.load_account.return_value = Mock()
        fake_server.simulate_transaction.return_value = fake_sim
        fake_server.send_transaction.return_value = {"errorResultXdr": "XDR error"}

        monkeypatch.setattr(kb, "SIGNING_BACKEND", "env_key")
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb, "Keypair", Mock(from_secret=lambda s: fake_keypair))
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        with pytest.raises(RuntimeError, match="Transaction submission failed"):
            kb.execute_rebalance_transaction(
                target_stable_pct=100.0,
                volatility_score=85.5,
                contract_id="CONTRACT_XYZ",
                source_secret=TEST_SECRET_KEY,
            )


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
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)

        # Score still high → target stays 100 % stable → delta == 0
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=90.0))
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_triggers_rebalance_when_delta_above_threshold(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0  # currently in risky

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_execute = Mock(return_value={"hash": "x"})
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_called_once()
        assert bot._last_allocation == 100.0

    def test_skips_submission_when_contract_id_missing(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_skips_submission_when_secret_missing(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", "")
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_handles_api_fetch_failure_gracefully(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(
            kb, "fetch_volatility_score",
            AsyncMock(side_effect=httpx.RequestError("timeout")),
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        # Should not raise
        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_handles_execution_failure_gracefully(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_execute = Mock(side_effect=RuntimeError("RPC down"))
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        # Should not raise
        _run(bot.run_once())

    def test_does_not_update_allocation_on_submission_failure(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(kb, "fetch_volatility_score", AsyncMock(return_value=85.0))
        mock_execute = Mock(side_effect=RuntimeError("RPC down"))
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        assert bot._last_allocation == 0.0  # unchanged after failed submission
