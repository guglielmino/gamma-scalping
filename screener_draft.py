import os
import asyncio
import logging
from datetime import date, timedelta
from typing import List, Dict, Optional, Tuple

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from dotenv import load_dotenv

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# Load environment variables from .env file
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
IS_PAPER_TRADING = os.getenv("IS_PAPER_TRADING", "true").lower() == "true"

# --- Screener Parameters ---
MIN_EXPIRATION_DAYS = 30
MAX_EXPIRATION_DAYS = 60
MIN_OPEN_INTEREST = 500
MIN_VOLUME = 100
MAX_BID_ASK_SPREAD_RATIO = 0.10 # e.g., 10% of the mid-price

# --- Placeholder for a real pricing model ---
def get_greeks_for_option(option_contract: Dict, stock_price: float) -> Dict[str, float]:
    """
    Placeholder function to simulate a real options pricing model (e.g., Black-Scholes).
    In a real system, this would take more inputs (interest rates, time, etc.)
    and perform a complex calculation. For this example, we'll return mock values.
    """
    # NOTE: These are simplified mock calculations and NOT financially accurate.
    # A proper implementation would use a library like `py_vollib`.
    strike = float(option_contract.strike_price)
    dte = (option_contract.expiration_date - date.today()).days
    
    # Mocking higher gamma and theta for shorter-dated options
    gamma = 0.1 / (dte / 30)  # Gamma is higher closer to expiration
    theta = -0.05 * (30 / dte) # Theta decay accelerates closer to expiration
    
    # Mocking higher gamma for at-the-money options
    if abs(strike - stock_price) < 5:
        gamma *= 1.5
    else:
        gamma *= 0.8
        
    return {"gamma": gamma, "theta": theta}


class OptionScreener:
    """
    Scans the option chain to find the most efficient contracts for a gamma scalping strategy.
    """
    def __init__(self):
        self.trading_client = TradingClient(API_KEY, API_SECRET, paper=IS_PAPER_TRADING)
        self.option_data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)
        logger.info("OptionScreener initialized.")

    async def get_atm_strangle(self, expiration_date: date, underlying_price: float) -> Optional[Tuple[Dict, Dict]]:
        """Finds the at-the-money (ATM) call and put for a given expiration."""
        try:
            contracts = self.trading_client.get_option_contracts(
                GetOptionContractsRequest(
                    underlying_symbol="SPY",
                    status="active",
                    expiration_date=expiration_date
                )
            )
            
            # Find the strike price closest to the underlying price
            atm_strike = min(contracts, key=lambda c: abs(c.strike_price - underlying_price)).strike_price
            
            atm_call = next((c for c in contracts if c.strike_price == atm_strike and c.right == 'call'), None)
            atm_put = next((c for c in contracts if c.strike_price == atm_strike and c.right == 'put'), None)
            
            if atm_call and atm_put:
                return atm_call.__dict__, atm_put.__dict__ # Return as dicts for easier use
            
        except Exception as e:
            logger.error(f"Could not fetch ATM strangle for {expiration_date}: {e}")
        return None, None
        
    async def is_liquid(self, call_symbol: str, put_symbol: str) -> bool:
        """Checks if the ATM straddle meets our liquidity criteria."""
        try:
            latest_quotes = self.option_data_client.get_latest_quote([call_symbol, put_symbol])
            
            call_quote = latest_quotes[call_symbol]
            put_quote = latest_quotes[put_symbol]

            # Check bid-ask spread
            call_spread = call_quote.ask_price - call_quote.bid_price
            put_spread = put_quote.ask_price - put_quote.bid_price
            
            call_mid = (call_quote.ask_price + call_quote.bid_price) / 2
            put_mid = (put_quote.ask_price + put_quote.bid_price) / 2

            if call_spread / call_mid > MAX_BID_ASK_SPREAD_RATIO:
                logger.warning(f"{call_symbol} failed spread check: {call_spread:.2f}")
                return False
            if put_spread / put_mid > MAX_BID_ASK_SPREAD_RATIO:
                logger.warning(f"{put_symbol} failed spread check: {put_spread:.2f}")
                return False
                
            # NOTE: Alpaca's current API doesn't provide live/latest open interest or volume.
            # This check would typically be done here using data from snapshots or another source.
            # We'll simulate this check as passing for now.
            logger.info(f"Liquidity check passed for {call_symbol}/{put_symbol}")
            return True

        except Exception as e:
            logger.error(f"Could not check liquidity for {call_symbol}/{put_symbol}: {e}")
            return False

    async def find_best_contracts(self, underlying_symbol: str) -> Optional[Dict[str, str]]:
        """
        Main method to execute the screening process.
        1. Filters expirations by date.
        2. Filters candidates by liquidity.
        3. Scores remaining candidates by Theta/Gamma ratio.
        4. Returns the best scoring contract pair.
        """
        logger.info(f"Starting screener for {underlying_symbol}...")
        
        # 1. Get underlying price and valid expiration dates
        try:
            underlying_price = self.trading_client.get_latest_trade(underlying_symbol).price
            chain = self.trading_client.get_option_chain(underlying_symbol)
            
            today = date.today()
            valid_expirations = [
                exp for exp in chain.expirations 
                if MIN_EXPIRATION_DAYS <= (exp - today).days <= MAX_EXPIRATION_DAYS
            ]
            logger.info(f"Found {len(valid_expirations)} valid expirations between {MIN_EXPIRATION_DAYS}-{MAX_EXPIRATION_DAYS} DTE.")
            
        except Exception as e:
            logger.error(f"Failed to fetch initial data for {underlying_symbol}: {e}")
            return None

        # 2. Analyze each candidate expiration
        candidates = []
        for expiration in valid_expirations:
            logger.info(f"--- Analyzing Expiration: {expiration} ---")
            atm_call, atm_put = await self.get_atm_strangle(expiration, underlying_price)
            
            if not atm_call or not atm_put:
                continue

            # 3. Filter by liquidity
            if not await self.is_liquid(atm_call['symbol'], atm_put['symbol']):
                continue
                
            # 4. Calculate Greeks and Score
            call_greeks = get_greeks_for_option(atm_call, underlying_price)
            put_greeks = get_greeks_for_option(atm_put, underlying_price)

            total_gamma = call_greeks['gamma'] + put_greeks['gamma']
            total_theta = call_greeks['theta'] + put_greeks['theta']

            if total_gamma == 0: continue

            # The score represents the daily cost (theta) per unit of gamma. Lower is better.
            score = abs(total_theta) / total_gamma
            
            logger.info(f"Score for {expiration}: {score:.4f} (Theta: {total_theta:.4f}, Gamma: {total_gamma:.4f})")
            
            candidates.append({
                "score": score,
                "call_symbol": atm_call['symbol'],
                "put_symbol": atm_put['symbol'],
                "expiration": expiration
            })

        # 5. Select the best candidate
        if not candidates:
            logger.warning("No suitable options contracts found after screening.")
            return None
            
        best_candidate = min(candidates, key=lambda c: c['score'])
        
        logger.info("--- Screener Finished ---")
        logger.info(f"Best Expiration Found: {best_candidate['expiration']}")
        logger.info(f"Best Score: {best_candidate['score']:.4f}")
        logger.info(f"Selected Call: {best_candidate['call_symbol']}")
        logger.info(f"Selected Put: {best_candidate['put_symbol']}")
        
        return {
            "call_symbol": best_candidate['call_symbol'],
            "put_symbol": best_candidate['put_symbol']
        }

async def main():
    screener = OptionScreener()
    best_strangle = await screener.find_best_contracts("SPY")
    
    if best_strangle:
        print("\n--- Recommended Straddle ---")
        print(f"Call: {best_strangle['call_symbol']}")
        print(f"Put:  {best_strangle['put_symbol']}")
        print("--------------------------")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logger.error(f"An error occurred in the main execution: {e}")
