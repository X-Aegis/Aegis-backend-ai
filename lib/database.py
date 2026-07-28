import os
from datetime import timedelta

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, RealDictCursor, execute_values

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

def get_connection():
    """Establishes a connection to the PostgreSQL/TimescaleDB database."""
    return psycopg2.connect(DATABASE_URL)

def save_fx_rates(rates):
    """
    Saves a list of FX rates to the database.
    Each rate should be a tuple (timestamp, pair, rate, source).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            execute_values(cur,
                "INSERT INTO fx_rates (timestamp, pair, rate, source) VALUES %s ON CONFLICT DO NOTHING",
                rates
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error saving FX rates: {e}")
        raise
    finally:
        conn.close()

def save_sentiment_data(records):
    """
    Saves a list of sentiment records to the database.
    Each record should be a tuple (timestamp, source, keyword, content, sentiment_score).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            execute_values(cur,
                "INSERT INTO sentiment_data (timestamp, source, keyword, content, sentiment_score) VALUES %s",
                records
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error saving sentiment data: {e}")
        raise
    finally:
        conn.close()

def get_fx_rate_series(pair, start_date, end_date):
    """
    Returns [(timestamp, rate), ...] for the given pair between start_date and
    end_date (inclusive of both calendar days), ordered by timestamp ascending.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT "timestamp", rate
                FROM fx_rates
                WHERE pair = %s AND "timestamp" >= %s AND "timestamp" < %s
                ORDER BY "timestamp" ASC
                """,
                (pair, start_date, end_date + timedelta(days=1)),
            )
            return cur.fetchall()
    finally:
        conn.close()

def save_backtest_result(strategy_name, pair, start_date, end_date, data_points_used,
                          params, strategy_metrics, baseline_metrics, comparison):
    """
    Persists a backtest report and returns its generated {id, created_at}.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO backtest_results
                    (strategy_name, pair, start_date, end_date, data_points_used,
                     params, strategy_metrics, baseline_metrics, comparison)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    strategy_name,
                    pair,
                    start_date,
                    end_date,
                    data_points_used,
                    Json(params),
                    Json(strategy_metrics),
                    Json(baseline_metrics),
                    Json(comparison),
                ),
            )
            result = cur.fetchone()
        conn.commit()
        return result
    except Exception as e:
        conn.rollback()
        print(f"Error saving backtest result: {e}")
        raise
    finally:
        conn.close()

def get_current_prediction(horizon: int = 1):
    """
    Returns the most recent prediction row for the given horizon as a dict, or
    None if no predictions exist yet.

    Columns returned: timestamp, horizon, volatility_score.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT "timestamp", horizon, volatility_score
                FROM predictions
                WHERE horizon = %s
                ORDER BY "timestamp" DESC
                LIMIT 1
                """,
                (horizon,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_prediction_history(
    horizon: int = 1,
    limit: int = 100,
    offset: int = 0,
):
    """
    Returns prediction rows for the given horizon, most recent first.

    Columns returned: timestamp, horizon, volatility_score.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT "timestamp", horizon, volatility_score
                FROM predictions
                WHERE horizon = %s
                ORDER BY "timestamp" DESC
                LIMIT %s OFFSET %s
                """,
                (horizon, limit, offset),
            )
            return cur.fetchall()
    finally:
        conn.close()


def list_backtest_results(pair=None, strategy_name=None, limit=20, offset=0):
    """
    Returns stored backtest reports ordered by most recent first, optionally
    filtered by pair and/or strategy_name.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            filters = []
            values = []
            if pair:
                filters.append('pair = %s')
                values.append(pair)
            if strategy_name:
                filters.append('strategy_name = %s')
                values.append(strategy_name)

            where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
            values.extend([limit, offset])

            cur.execute(
                f"""
                SELECT id, created_at, strategy_name, pair, start_date, end_date,
                       data_points_used, params, strategy_metrics, baseline_metrics, comparison
                FROM backtest_results
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                values,
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Drift monitoring helpers
# ---------------------------------------------------------------------------

def save_prediction(timestamp, horizon: int, volatility_score: float, pair: str = "USD/NGN"):
    """
    Inserts a live model prediction into the predictions table.
    actual_outcome is left NULL and can be filled in later via
    :func:`record_actual_outcome`.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO predictions (timestamp, horizon, volatility_score, pair)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (timestamp, horizon) DO UPDATE
                    SET volatility_score = EXCLUDED.volatility_score,
                        pair             = EXCLUDED.pair
                """,
                (timestamp, horizon, volatility_score, pair),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error saving prediction: {e}")
        raise
    finally:
        conn.close()


def record_actual_outcome(timestamp, horizon: int, actual_outcome: float):
    """
    Back-fills the actual observed volatility score for an existing prediction
    row identified by (timestamp, horizon).

    Returns the number of rows updated (0 if the prediction was not found).
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE predictions
                SET actual_outcome = %s
                WHERE timestamp = %s AND horizon = %s
                """,
                (actual_outcome, timestamp, horizon),
            )
            updated = cur.rowcount
        conn.commit()
        return updated
    except Exception as e:
        conn.rollback()
        print(f"Error recording actual outcome: {e}")
        raise
    finally:
        conn.close()


def get_predictions_with_actuals(pair: str, horizon: int, limit: int = 200):
    """
    Returns rows from predictions that have both a volatility_score AND an
    actual_outcome, ordered oldest-first (so callers can replay them through a
    drift monitor in chronological order).

    Each row dict has keys: timestamp, horizon, volatility_score, actual_outcome, pair.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT timestamp, horizon, volatility_score, actual_outcome, pair
                FROM predictions
                WHERE pair = %s
                  AND horizon = %s
                  AND actual_outcome IS NOT NULL
                ORDER BY timestamp ASC
                LIMIT %s
                """,
                (pair, horizon, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def save_drift_event(
    pair: str,
    horizon: int,
    predicted: float,
    actual: float,
    abs_error: float,
    rolling_mae,
    rolling_rmse,
    adwin_drift_detected: bool,
    ph_drift_detected: bool,
    ph_statistic: float,
    adwin_window_size: int,
):
    """
    Persists a single drift-detector result to the drift_events table.
    The ``timestamp`` column defaults to ``now()`` on the database side.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO drift_events (
                    pair, horizon, predicted, actual, abs_error,
                    rolling_mae, rolling_rmse,
                    adwin_drift_detected, ph_drift_detected,
                    ph_statistic, adwin_window_size
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    pair,
                    horizon,
                    predicted,
                    actual,
                    abs_error,
                    rolling_mae,
                    rolling_rmse,
                    adwin_drift_detected,
                    ph_drift_detected,
                    ph_statistic,
                    adwin_window_size,
                ),
            )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Error saving drift event: {e}")
        raise
    finally:
        conn.close()


def get_drift_summary(pair: str, horizon: int, limit: int = 100):
    """
    Returns the *limit* most recent drift events for (pair, horizon), ordered
    most-recent first.

    Each row dict has keys:
        timestamp, pair, horizon, predicted, actual, abs_error,
        rolling_mae, rolling_rmse, adwin_drift_detected, ph_drift_detected,
        ph_statistic, adwin_window_size.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT timestamp, pair, horizon,
                       predicted, actual, abs_error,
                       rolling_mae, rolling_rmse,
                       adwin_drift_detected, ph_drift_detected,
                       ph_statistic, adwin_window_size
                FROM drift_events
                WHERE pair = %s AND horizon = %s
                ORDER BY timestamp DESC
                LIMIT %s
                """,
                (pair, horizon, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()
