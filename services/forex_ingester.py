import os
import httpx
import asyncio
from datetime import datetime, timezone
import sys

# Add project root to sys.path for local imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.database import save_fx_rates

FIXER_API_KEY = os.getenv("FIXER_API_KEY")
OPEN_EXCHANGE_RATES_APP_ID = os.getenv("OPEN_EXCHANGE_RATES_APP_ID")

# Target currencies
CURRENCIES = ["NGN", "KES", "GHS", "ZAR"]
BASE_CURRENCY = "USD"

async def fetch_fixer_rates():
    """Fetches rates from Fixer.io."""
    if not FIXER_API_KEY:
        print("Fixer.io API key not found.")
        return []
    
    url = f"http://data.fixer.io/api/latest?access_key={FIXER_API_KEY}&symbols={','.join(CURRENCIES)}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            data = response.json()
            if not data.get("success"):
                print(f"Fixer.io API error: {data.get('error')}")
                return []
            
            timestamp = datetime.fromtimestamp(data["timestamp"], tz=timezone.utc)
            rates = []
            for symbol, rate in data["rates"].items():
                rates.append((timestamp, f"{BASE_CURRENCY}/{symbol}", rate, "Fixer.io"))
            return rates
    except Exception as e:
        print(f"Error fetching from Fixer.io: {e}")
        return []

async def fetch_open_exchange_rates():
    """Fetches rates from Open Exchange Rates."""
    if not OPEN_EXCHANGE_RATES_APP_ID:
        print("Open Exchange Rates App ID not found.")
        return []
    
    url = f"https://openexchangerates.org/api/latest.json?app_id={OPEN_EXCHANGE_RATES_APP_ID}&symbols={','.join(CURRENCIES)}"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            data = response.json()
            
            timestamp = datetime.fromtimestamp(data["timestamp"], tz=timezone.utc)
            rates = []
            for symbol, rate in data["rates"].items():
                rates.append((timestamp, f"{BASE_CURRENCY}/{symbol}", rate, "OpenExchangeRates"))
            return rates
    except Exception as e:
        print(f"Error fetching from Open Exchange Rates: {e}")
        return []

async def fetch_binance_p2p_rates():
    """
    Placeholder for Binance P2P scraping.
    Scraping P2P requires complex handling (browser automation or specific API endpoints).
    For now, we implement a log entry and return empty.
    """
    print("Binance P2P scraping ingestion is currently a placeholder.")
    return []

async def run_ingestion():
    """Orchestrates the ingestion process."""
    print(f"Starting Forex data ingestion at {datetime.now(timezone.utc)}")
    
    tasks = [
        fetch_fixer_rates(),
        fetch_open_exchange_rates(),
        fetch_binance_p2p_rates()
    ]
    
    results = await asyncio.gather(*tasks)
    all_rates = [rate for result in results for rate in result]
    
    if all_rates:
        print(f"Fetched {len(all_rates)} rates. Saving to database...")
        save_fx_rates(all_rates)
        print("Ingestion successful.")
    else:
        print("No rates fetched.")

if __name__ == "__main__":
    asyncio.run(run_ingestion())
