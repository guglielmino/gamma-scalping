import datetime
from config import API_KEY, API_SECRET, HEDGING_ASSET
from alpaca.data.historical.corporate_actions import CorporateActionsClient
from alpaca.data import StockHistoricalDataClient
from alpaca.data.enums import CorporateActionsType
from alpaca.data.requests import CorporateActionsRequest, StockLatestQuoteRequest
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

def get_dividend_yield():
    """
    Calculates the annualized dividend yield for the HEDGING_ASSET.
    It fetches all cash dividends over the last 365 days, sums them up,
    and divides by the current price of the asset.
    """
    logger.info(f"Starting dividend yield calculation for {HEDGING_ASSET}")
    try:
        actions_client = CorporateActionsClient(API_KEY, API_SECRET)
        stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)

        end_date = datetime.datetime.now().date()
        start_date = end_date - datetime.timedelta(days=365)

        actions_request = CorporateActionsRequest(
            symbols=[HEDGING_ASSET],
            types=[CorporateActionsType.CASH_DIVIDEND],
            start=start_date,
            end=end_date
        )
        logger.info(f"Fetching dividends for {HEDGING_ASSET} from {start_date} to {end_date}")

        actions = actions_client.get_corporate_actions(actions_request)
        
        # Check if any cash dividends were returned
        cash_dividends = actions.data.get("cash_dividends", [])
        if not cash_dividends:
            logger.warning(f"No cash dividends found for {HEDGING_ASSET} in the last year. Yield is 0%.")
            return 0.0

        logger.info(f"Found {len(cash_dividends)} cash dividend(s).")

        # Get the latest quote
        logger.info(f"Fetching latest price for {HEDGING_ASSET}...")
        quote_request = StockLatestQuoteRequest(symbol_or_symbols=HEDGING_ASSET)
        latest_quote = stock_client.get_stock_latest_quote(quote_request)
        current_price = (latest_quote[HEDGING_ASSET].ask_price + latest_quote[HEDGING_ASSET].bid_price) / 2
        logger.info(f"Latest mid-price for {HEDGING_ASSET}: ${current_price:.2f}")

        # Sum the dividend rates
        dividends_ytd = sum(action.rate for action in cash_dividends)
        logger.info(f"Total annual dividend per share: ${dividends_ytd:.4f}")

        if current_price == 0:
            logger.error("Current price is $0, cannot calculate yield. Returning 0.0.")
            return 0.0
            
        dividend_yield = dividends_ytd / current_price
        logger.info(f"Calculated dividend yield for {HEDGING_ASSET}: {dividend_yield:.4%}")
        
        return dividend_yield

    except Exception as e:
        logger.error(f"Failed to calculate dividend yield for {HEDGING_ASSET}: {e}")
        return 0.0
