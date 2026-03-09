import os
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

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
