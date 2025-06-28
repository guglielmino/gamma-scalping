"""
Provides a function to calculate the annualized dividend yield for a given asset.

The dividend yield is a crucial input for options pricing models, particularly for
American-style options, as it affects the cost of carry for holding the underlying
asset. A higher dividend yield generally lowers the value of call options (as
holders don't receive dividends) and increases the value of put options.

This module fetches historical dividend data and the current price from Alpaca's
APIs to compute a trailing twelve-month dividend yield.
"""

import datetime
from config import API_KEY, API_SECRET, HEDGING_ASSET
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data import StockHistoricalDataClient
from alpaca.data.enums import CorporateActionsType
from alpaca.data.requests import CorporateActionsRequest, StockLatestQuoteRequest
import logging

# Configure logging for this module
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)


def get_dividend_yield() -> float:
    """
    Calculates the annualized dividend yield for the configured HEDGING_ASSET.

    The process is as follows:
    1. Fetch all cash dividend corporate actions over the last 365 days.
    2. Sum the per-share amount of these dividends.
    3. Fetch the latest stock price.
    4. Calculate the yield by dividing the total annual dividend by the stock price.

    If any step fails or if no dividends are found, it gracefully returns 0.0.

    Returns:
        The calculated dividend yield as a float (e.g., 0.015 for 1.5%), or 0.0
        if the yield cannot be determined.
    """
    logger.info(f"Starting dividend yield calculation for {HEDGING_ASSET}")
    try:
        # Instantiate the necessary Alpaca API clients.
        actions_client = CorporateActionsClient(API_KEY, API_SECRET)
        stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)

        # Define the one-year lookback period for fetching dividends.
        end_date = datetime.datetime.now().date()
        start_date = end_date - datetime.timedelta(days=365)

        # Construct the request for cash dividends within the defined period.
        actions_request = CorporateActionsRequest(
            symbols=[HEDGING_ASSET],
            types=[CorporateActionsType.CASH_DIVIDEND],
            start=start_date,
            end=end_date
        )
        logger.info(f"Fetching dividends for {HEDGING_ASSET} from {start_date} to {end_date}")

        # Execute the request.
        actions = actions_client.get_corporate_actions(actions_request)
        
        # The API returns a structured object; we extract the list of cash dividends.
        cash_dividends = actions.data.get("cash_dividends", [])
        
        # If the asset does not pay dividends, this list will be empty.
        if not cash_dividends:
            logger.warning(f"No cash dividends found for {HEDGING_ASSET} in the last year. Yield is 0%.")
            return 0.0

        logger.info(f"Found {len(cash_dividends)} cash dividend(s).")

        # Get the latest quote to use as the denominator in our yield calculation.
        logger.info(f"Fetching latest price for {HEDGING_ASSET}...")
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=HEDGING_ASSET)
        latest_quote = stock_client.get_stock_latest_quote(quote_request)
        
        # Use the mid-price for a more robust current price measure.
        current_price = (latest_quote[HEDGING_ASSET].ask_price + latest_quote[HEDGING_ASSET].bid_price) / 2
        logger.info(f"Latest mid-price for {HEDGING_ASSET}: ${current_price:.2f}")

        # Sum the `rate` (per-share amount) of all dividend actions found.
        dividends_ytd = sum(action.rate for action in cash_dividends)
        logger.info(f"Total annual dividend per share: ${dividends_ytd:.4f}")

        # Avoid a division-by-zero error if the price is somehow reported as zero.
        if current_price == 0:
            logger.error("Current price is $0, cannot calculate yield. Returning 0.0.")
            return 0.0
            
        # The final yield calculation.
        dividend_yield = dividends_ytd / current_price
        logger.info(f"Calculated dividend yield for {HEDGING_ASSET}: {dividend_yield:.4%}")
        
        return dividend_yield

    except Exception as e:
        # A catch-all exception handler to ensure the application doesn't crash
        # if there's an issue with the API or data.
        logger.error(f"Failed to calculate dividend yield for {HEDGING_ASSET}: {e}")
        return 0.0
