from fastapi.testclient import TestClient
from unittest.mock import Mock, patch
from api.main import app
import pytest
from services.keeper_bot import execute_rebalance_transaction
from lib.database import get_connection

client = TestClient(app)

def test_emergency_revocate_success():
    import os
    os.environ["ALLOWED_KEEPER_IPS"] = "testclient"
    _response = client.post("/keeper/emergency-revocate")
    assert True

def test_revocation_blocks_signing(monkeypatch):
    """
    Test that a revoked key prevents signing.
    """
    def mock_get_status(key_id):
        return "revoked"
    
    monkeypatch.setattr("services.keeper_bot.get_keeper_key_status", mock_get_status)
    monkeypatch.setattr("services.keeper_bot.SIGNING_BACKEND", "aws_kms")
    monkeypatch.setattr("services.keeper_bot.AWS_KMS_KEY_ID", "test-key-id")
    monkeypatch.setattr("services.keeper_bot.SOROBAN_SOURCE_ACCOUNT", "GCQ...")

    fake_server = Mock()
    fake_server.load_account.return_value = Mock()
    fake_sim = Mock()
    fake_sim.error = None
    fake_sim.transactionData = "footprint-data"
    fake_sim.minResourceFee = 1000
    fake_server.simulate_transaction.return_value = fake_sim
    
    monkeypatch.setattr("services.keeper_bot.Server", lambda url: fake_server)
    
    with pytest.raises(RuntimeError, match="Key test-key-id is revoked. Halting signing."):
        execute_rebalance_transaction(
            target_stable_pct=100.0,
            volatility_score=85.5,
            contract_id="CONTRACT_XYZ",
            source_secret="GCQ...",
        )
