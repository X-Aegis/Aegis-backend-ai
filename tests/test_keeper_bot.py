"""
Tests for services/keeper_bot.py
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stellar_sdk import Keypair as StellarKeypair

import services.keeper_bot as kb

TEST_KEYPAIR = StellarKeypair.random()
TEST_PUBLIC_KEY = TEST_KEYPAIR.public_key
TEST_SECRET_KEY = TEST_KEYPAIR.secret

_TS_NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)
_TS_LATER = _TS_NOW + timedelta(hours=1)


def _run(coro):
    return asyncio.run(coro)


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
# KeeperBot._should_rebalance
# ---------------------------------------------------------------------------

class TestShouldRebalance:
    def test_triggers_on_score_above_cutoff(self):
        bot = kb.KeeperBot()
        bot._last_allocation = 0.0
        assert bot._should_rebalance(score=85.0, target=100.0, signal_ts=_TS_NOW)

    def test_triggers_on_allocation_delta_above_threshold(self, monkeypatch):
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        bot = kb.KeeperBot()
        bot._last_allocation = 0.0
        # Score below cutoff, but allocation delta is 100% (0 → 100)
        assert bot._should_rebalance(score=50.0, target=100.0, signal_ts=_TS_NOW)

    def test_skips_when_allocation_delta_below_threshold(self, monkeypatch):
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        bot = kb.KeeperBot()
        bot._last_allocation = 100.0
        # Score below cutoff, allocation delta is 0
        assert not bot._should_rebalance(score=50.0, target=100.0, signal_ts=_TS_NOW)

    def test_skips_when_same_signal_window(self, monkeypatch):
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        bot = kb.KeeperBot()
        bot._last_signal_window = _TS_NOW
        # Same timestamp → should skip
        assert not bot._should_rebalance(score=90.0, target=100.0, signal_ts=_TS_NOW)

    def test_skips_when_older_signal_window(self, monkeypatch):
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        bot = kb.KeeperBot()
        bot._last_signal_window = _TS_LATER
        # Older timestamp → should skip
        assert not bot._should_rebalance(score=90.0, target=100.0, signal_ts=_TS_NOW)

    def test_triggers_on_newer_signal_window(self, monkeypatch):
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        bot = kb.KeeperBot()
        bot._last_signal_window = _TS_NOW
        # Newer timestamp → should trigger
        assert bot._should_rebalance(score=90.0, target=100.0, signal_ts=_TS_LATER)

    def test_first_run_always_triggers(self):
        bot = kb.KeeperBot()
        assert bot._should_rebalance(score=30.0, target=0.0, signal_ts=_TS_NOW)


# ---------------------------------------------------------------------------
# KeeperBot._submit_with_retry
# ---------------------------------------------------------------------------

class TestSubmitWithRetry:
    def test_returns_on_first_success(self, monkeypatch):
        bot = kb.KeeperBot()
        fake_result = {"hash": "tx_success"}
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value=fake_result)
        )

        result = _run(
            bot._submit_with_retry(
                target_stable_pct=100.0,
                volatility_score=85.0,
                contract_id="C",
                source_secret=TEST_SECRET_KEY,
            )
        )
        assert result == fake_result

    def test_retries_on_failure_then_succeeds(self, monkeypatch):
        bot = kb.KeeperBot()
        call_count = 0
        fake_result = {"hash": "tx_success"}

        def _mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("RPC timeout")
            return fake_result

        monkeypatch.setattr(kb, "execute_rebalance_transaction", _mock_execute)
        monkeypatch.setattr(kb, "MAX_RETRIES", 3)
        monkeypatch.setattr(kb, "RETRY_BACKOFF_BASE", 0.01)  # fast for tests

        result = _run(
            bot._submit_with_retry(
                target_stable_pct=100.0,
                volatility_score=85.0,
                contract_id="C",
                source_secret=TEST_SECRET_KEY,
            )
        )
        assert result == fake_result
        assert call_count == 3

    def test_returns_none_after_all_retries_exhausted(self, monkeypatch):
        bot = kb.KeeperBot()
        monkeypatch.setattr(
            kb,
            "execute_rebalance_transaction",
            Mock(side_effect=RuntimeError("RPC down")),
        )
        monkeypatch.setattr(kb, "MAX_RETRIES", 2)
        monkeypatch.setattr(kb, "RETRY_BACKOFF_BASE", 0.01)

        result = _run(
            bot._submit_with_retry(
                target_stable_pct=100.0,
                volatility_score=85.0,
                contract_id="C",
                source_secret=TEST_SECRET_KEY,
            )
        )
        assert result is None

    def test_logs_tx_hash_on_success(self, monkeypatch, caplog):
        bot = kb.KeeperBot()
        fake_result = {"hash": "abc123def"}
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value=fake_result)
        )

        with caplog.at_level("INFO"):
            _run(
                bot._submit_with_retry(
                    target_stable_pct=100.0,
                    volatility_score=85.0,
                    contract_id="C",
                    source_secret=TEST_SECRET_KEY,
                )
            )
        assert any("tx_hash=abc123def" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# KeeperBot.run_once
# ---------------------------------------------------------------------------


class TestKeeperBotRunOnce:
    @pytest.fixture(autouse=True)
    def setup_mocks(self, monkeypatch):
        monkeypatch.setattr(kb, "get_keeper_status", lambda: None)
        monkeypatch.setattr(kb, "get_manual_override", lambda: False)
        monkeypatch.setattr(kb, "get_last_submitted_allocation", lambda: None)
        monkeypatch.setattr(
            kb,
            "get_keeper_stats",
            lambda: {
                "count_last_24h": 0,
                "last_rebalance_time": None,
                "last_10_decisions": [],
            },
        )
        monkeypatch.setattr(kb, "record_keeper_heartbeat", Mock())
        monkeypatch.setattr(kb, "record_keeper_failure", Mock())
        monkeypatch.setattr(kb, "record_keeper_decision", Mock())
        # BK-11 secure key management
        monkeypatch.setattr(kb, "assert_signing_allowed", lambda: None)
        monkeypatch.setattr(kb, "record_signing_event", Mock())
        monkeypatch.setattr(kb, "get_active_signing_config", lambda: None)

    def _bot(self):
        return kb.KeeperBot()

    def test_skips_when_delta_below_threshold(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0  # currently in risky allocation

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)

        # Score below cutoff → target stays 0 % stable → delta == 0 < threshold
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(50.0, _TS_NOW))
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_triggers_rebalance_when_delta_above_threshold(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock(return_value={"hash": "x"})
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_called_once()
        assert bot._last_allocation == 100.0

    def test_triggers_rebalance_when_score_above_cutoff(self, monkeypatch):
        """Score >= HIGH_VOLATILITY_CUTOFF triggers rebalance even with no allocation delta."""
        bot = self._bot()
        bot._last_allocation = 100.0  # already at target allocation

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_LATER))
        )
        mock_execute = Mock(return_value={"hash": "x"})
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_called_once()

    def test_skips_submission_when_contract_id_missing(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_skips_submission_when_secret_missing(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", "")
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_handles_api_fetch_failure_gracefully(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(
            kb,
            "fetch_volatility_score",
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
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(kb, "MAX_RETRIES", 1)
        monkeypatch.setattr(kb, "RETRY_BACKOFF_BASE", 0.01)
        mock_execute = Mock(side_effect=RuntimeError("RPC down"))
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        # Should not raise
        _run(bot.run_once())

    def test_does_not_update_allocation_on_submission_failure(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(kb, "MAX_RETRIES", 1)
        monkeypatch.setattr(kb, "RETRY_BACKOFF_BASE", 0.01)
        mock_execute = Mock(side_effect=RuntimeError("RPC down"))
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        assert bot._last_allocation == 90.0  # unchanged after failed submission

    def test_halts_when_circuit_breaker_tripped(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(
            kb,
            "get_keeper_status",
            lambda: {
                "consecutive_failures": 3,
                "last_heartbeat": kb.datetime.now(kb.timezone.utc),
            },
        )
        mock_fetch = AsyncMock()
        monkeypatch.setattr(kb, "fetch_volatility_score", mock_fetch)
        _run(bot.run_once())
        mock_fetch.assert_not_called()

    def test_halts_when_dead_man_active(self, monkeypatch):
        bot = self._bot()

        past = kb.datetime.now(kb.timezone.utc) - timedelta(hours=7)
        monkeypatch.setattr(
            kb,
            "get_keeper_status",
            lambda: {"consecutive_failures": 0, "last_heartbeat": past},
        )
        mock_fetch = AsyncMock()
        monkeypatch.setattr(kb, "fetch_volatility_score", mock_fetch)
        _run(bot.run_once())
        mock_fetch.assert_not_called()

    def test_records_heartbeat_on_success(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(
            kb,
            "get_keeper_status",
            lambda: {
                "consecutive_failures": 0,
                "last_heartbeat": kb.datetime.now(kb.timezone.utc),
            },
        )
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value={"hash": "x"})
        )
        mock_heartbeat = Mock()
        monkeypatch.setattr(kb, "record_keeper_heartbeat", mock_heartbeat)

        _run(bot.run_once())
        mock_heartbeat.assert_called_once()

    def test_records_failure_on_exception(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(
            kb,
            "get_keeper_status",
            lambda: {
                "consecutive_failures": 0,
                "last_heartbeat": kb.datetime.now(kb.timezone.utc),
            },
        )
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(side_effect=RuntimeError("fail"))
        )
        mock_failure = Mock()
        monkeypatch.setattr(kb, "record_keeper_failure", mock_failure)

        _run(bot.run_once())
        mock_failure.assert_called_once()

    def test_rejects_when_rate_limit_exceeded(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "get_keeper_stats", lambda: {"count_last_24h": 4})
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock()
        mock_decision = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)
        monkeypatch.setattr(kb, "record_keeper_decision", mock_decision)

        _run(bot.run_once())

        mock_execute.assert_not_called()
        assert mock_decision.call_args.args[3] == "rejected"
        assert mock_decision.call_args.args[2]["rate_limit"]["passed"] is False

    def test_rejects_when_allocation_change_exceeds_limit(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock()
        mock_decision = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)
        monkeypatch.setattr(kb, "record_keeper_decision", mock_decision)

        _run(bot.run_once())

        mock_execute.assert_not_called()
        assert mock_decision.call_args.args[2]["allocation_change"]["passed"] is False

    def test_uses_last_submitted_allocation_after_restart(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "get_last_submitted_allocation", lambda: 0.0)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())

        mock_execute.assert_not_called()

    def test_manual_override_bypasses_policy_guards(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 0.0
        monkeypatch.setattr(kb, "get_manual_override", lambda: True)
        monkeypatch.setattr(kb, "get_keeper_stats", lambda: {"count_last_24h": 4})
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        mock_execute = Mock(return_value={"hash": "x"})
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())

        mock_execute.assert_called_once()

    def test_records_auditable_decision_format(self, monkeypatch):
        bot = self._bot()
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value={"hash": "x"})
        )
        mock_decision = Mock()
        monkeypatch.setattr(kb, "record_keeper_decision", mock_decision)

        _run(bot.run_once())

        score, allocations, checks, decision = mock_decision.call_args.args[:4]
        assert score == 85.0
        assert allocations == {"stable": 100.0, "risky": 0.0}
        assert {
            "rate_limit",
            "allocation_change",
            "rebalance_threshold",
        } <= checks.keys()
        assert decision == "approved"

    # -- BK-11: revocation blocks signing, audit log on success --------------

    def test_revoked_signing_key_blocks_submission(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )

        def _revoked():
            raise kb.SigningKeyRevokedError("key alias/x was revoked at ...: leak")

        monkeypatch.setattr(kb, "assert_signing_allowed", _revoked)
        mock_execute = Mock()
        mock_decision = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)
        monkeypatch.setattr(kb, "record_keeper_decision", mock_decision)

        _run(bot.run_once())

        mock_execute.assert_not_called()
        assert mock_decision.call_args.args[3] == "rejected"
        assert mock_decision.call_args.args[2]["signing_key"]["passed"] is False

    def test_records_signing_audit_on_success(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb,
            "execute_rebalance_transaction",
            Mock(return_value={"hash": "deadbeef"}),
        )
        monkeypatch.setattr(
            kb,
            "get_active_signing_config",
            lambda: {"key_id": "alias/aegis-keeper-2026Q3"},
        )
        mock_audit = Mock()
        monkeypatch.setattr(kb, "record_signing_event", mock_audit)

        _run(bot.run_once())

        mock_audit.assert_called_once_with(
            "deadbeef", "alias/aegis-keeper-2026Q3", actor="keeper_bot"
        )

    def test_signing_audit_failure_does_not_crash_loop(self, monkeypatch):
        bot = self._bot()
        bot._last_allocation = 90.0
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(85.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value={"hash": "x"})
        )
        monkeypatch.setattr(
            kb, "record_signing_event", Mock(side_effect=RuntimeError("db down"))
        )
        mock_heartbeat = Mock()
        monkeypatch.setattr(kb, "record_keeper_heartbeat", mock_heartbeat)

        _run(bot.run_once())  # must not raise
        mock_heartbeat.assert_called_once()
        assert bot._last_allocation == 100.0

    # -- Idempotency tests (BK-5) --------------------------------------------

    def test_idempotent_skips_same_signal_window(self, monkeypatch):
        """Second run with same signal timestamp should not rebalance."""
        bot = self._bot()
        bot._last_allocation = 0.0
        bot._last_signal_window = _TS_NOW

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(90.0, _TS_NOW))
        )
        mock_execute = Mock()
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_not_called()

    def test_idempotent_allows_newer_signal_window(self, monkeypatch):
        """Rebalance should proceed when a newer signal window arrives."""
        bot = self._bot()
        bot._last_allocation = 0.0
        bot._last_signal_window = _TS_NOW

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "MAX_ALLOCATION_CHANGE_PCT", 200.0)  # bypass guard
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(90.0, _TS_LATER))
        )
        mock_execute = Mock(return_value={"hash": "x"})
        monkeypatch.setattr(kb, "execute_rebalance_transaction", mock_execute)

        _run(bot.run_once())
        mock_execute.assert_called_once()

    # -- Retry tests (BK-5) ---------------------------------------------------

    def test_retries_on_tx_failure(self, monkeypatch):
        """KeeperBot.run_once should retry failed transactions."""
        bot = self._bot()
        call_count = 0
        fake_result = {"hash": "retry_success"}

        def _mock_execute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("Temporary RPC error")
            return fake_result

        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(kb, "MAX_RETRIES", 3)
        monkeypatch.setattr(kb, "RETRY_BACKOFF_BASE", 0.01)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(90.0, _TS_NOW))
        )
        monkeypatch.setattr(kb, "execute_rebalance_transaction", _mock_execute)

        _run(bot.run_once())
        assert call_count == 2
        assert bot._last_allocation == 100.0

    def test_logs_tx_hash_on_success(self, monkeypatch, caplog):
        """Successful rebalance should log tx hash."""
        bot = self._bot()
        monkeypatch.setattr(kb, "REBALANCE_THRESHOLD", 5.0)
        monkeypatch.setattr(kb, "HIGH_VOLATILITY_CUTOFF", 80.0)
        monkeypatch.setattr(kb, "SOROBAN_CONTRACT_ID", "C")
        monkeypatch.setattr(kb, "ADMIN_SECRET_KEY", TEST_SECRET_KEY)
        monkeypatch.setattr(
            kb, "fetch_volatility_score", AsyncMock(return_value=(90.0, _TS_NOW))
        )
        monkeypatch.setattr(
            kb, "execute_rebalance_transaction", Mock(return_value={"hash": "tx123"})
        )

        with caplog.at_level("INFO"):
            _run(bot.run_once())
        assert any("tx_hash=tx123" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# fetch_volatility_score
# ---------------------------------------------------------------------------

class TestFetchVolatilityScore:
    def test_returns_score_and_timestamp(self, monkeypatch):
        fake_response = Mock()
        fake_response.json.return_value = {
            "volatility_score": 72.5,
            "risk_level": "MEDIUM",
            "timestamp": "2026-08-22T12:00:00Z",
            "horizon": 1,
        }
        fake_response.raise_for_status = Mock()

        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_response)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: fake_client)

        score, ts = _run(kb.fetch_volatility_score())
        assert score == 72.5
        assert ts.year == 2026
        assert ts.month == 8

    def test_raises_on_http_error(self, monkeypatch):
        fake_response = Mock()
        fake_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=Mock(), response=Mock()
        )

        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_response)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: fake_client)

        with pytest.raises(httpx.HTTPStatusError):
            _run(kb.fetch_volatility_score())


# ---------------------------------------------------------------------------
# ExecuteRebalanceEnterpriseBackends
# ---------------------------------------------------------------------------

class TestExecuteRebalanceEnterpriseBackends:
    def test_vault_backend_resolves_seed_via_key_manager(self, monkeypatch):
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
        fake_server.send_transaction.return_value = {"hash": "vaulthash"}

        monkeypatch.setattr(kb, "SIGNING_BACKEND", "vault")
        monkeypatch.setattr(kb, "Server", lambda url: fake_server)
        monkeypatch.setattr(kb, "Keypair", Mock(from_secret=lambda s: fake_keypair))
        monkeypatch.setattr(kb, "TransactionBuilder", Mock())
        kb.TransactionBuilder.return_value.append_invoke_contract_function_op.return_value.set_timeout.return_value.build.return_value = fake_tx

        fake_manager = Mock()
        fake_manager.resolve_secret.return_value = TEST_SECRET_KEY
        monkeypatch.setattr(kb, "SigningKeyManager", lambda backend: fake_manager)

        result = kb.execute_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_secret="",  # ignored for vault backend
        )

        fake_manager.resolve_secret.assert_called_once()
        assert result == {"hash": "vaulthash"}
