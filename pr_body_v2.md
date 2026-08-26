📝 **Description**
Implement automatic failure detection and a dead-man switch for the keeper bot to prevent silent failures from compromising vault safety. 

### 🛑 The Problem
Prior to these changes, if the Keeper Bot failed silently (e.g. RPC crashes or hangs), the system would not detect this stagnation. The vault would continue relying on stale conditions without any active monitoring or rebalancing taking place, endangering overall safety protocols.

### 🛠 The Solution (What's Changed)

#### 1. Database Migrations & Status Tracking
- Added a new `keeper_status` table to centrally store the bot's health state: `consecutive_failures` and `last_heartbeat`.
- Built robust helper functions (`get_keeper_status`, `record_keeper_heartbeat`, `record_keeper_failure`, `reset_keeper_circuit`) in `lib/database.py` to seamlessly track executions and exceptions.

#### 2. Keeper Bot Integration
- **Dead-Man Switch**: Before executing a rebalance, the Keeper Bot checks if it has been strictly over 6 hours since the last successful heartbeat. If so, it suspends operations to prevent operating on extremely stale state contexts. 
- **Circuit Breaker**: If `consecutive_failures` reaches 3, the bot assumes there is a critical systemic issue (e.g., RPC completely down) and halts. 
- Rebalance executions (`execute_rebalance_transaction`) are now strictly wrapped in resilient try/except blocks to record these metrics accurately.

#### 3. Administrative Endpoints
- **`GET /keeper/status`**: Dynamically calculates the state (`OK`, `TRIPPED`, `DEAD_MAN_ACTIVE`) and returns real-time metrics on consecutive errors and heartbeats.
- **`POST /keeper/restart_circuit`**: Allows authorized resetting of the circuit breaker values once issues have been safely diagnosed.

#### 4. Hardened Type Checking
- Verified and fixed Python 3.9 compatible annotations (`typing.Optional`) on prior codebase files to ensure broad stability without runtime exceptions in Pydantic models.

### ✅ Acceptance Criteria Met
- Implemented circuit breaker (halts after 3 failures).
- Implemented dead-man switch (halts if >6h without heartbeat).
- Added `GET /keeper/status` endpoint.
- Added `POST /keeper/restart_circuit` admin endpoint.
- Comprehensive integration tests written and passing locally (`pytest`, `ruff format`, `ruff check`).

---
Closes #34
cc @bbkenny
