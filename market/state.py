# delta_hedger/market/state.py

import asyncio
import time
import logging
from clients.user_agent_mixin import OptionDataStreamSigned, StockDataStreamSigned
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    PRICE_CHANGE_THRESHOLD, HEARTBEAT_TRIGGER_SECONDS
)
from utils.parsing import parse_option_symbol
from .us_treasury_yield_curve import get_risk_free_rate
from .dividends import get_dividend_yield
from datetime import datetime

# Configure logging for this module
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)


class MarketDataManager:
    """
    Acts as the market sensing organ of the trading bot.

    This class is responsible for:
    - Subscribing to real-time stock and option quote data from Alpaca.
    - Maintaining the current, most up-to-date prices for the underlying asset
      and the two option contracts that form our straddle.
    - Implementing a filtering mechanism for stock quotes to discard potentially
      erroneous data points caused by wide bid-ask spreads.
    - Determining when to trigger a portfolio delta recalculation. This is a
      critical function to ensure the system is responsive but not hyperactive.
      Triggers are sent to the DeltaEngine via a dedicated asyncio.Queue.
    """

    def __init__(self, trigger_queue: asyncio.Queue, call_option_symbol: str, put_option_symbol: str):
        """
        Initializes the MarketDataManager.

        Args:
            trigger_queue: An asyncio.Queue used to send trigger signals to the DeltaEngine.
            call_option_symbol: The symbol for the call option of the straddle.
            put_option_symbol: The symbol for the put option of the straddle.
        """
        self.trigger_queue = trigger_queue
        self.call_option_symbol = call_option_symbol
        self.put_option_symbol = put_option_symbol

        # Parse option symbols to extract expiry and strike, which are needed for pricing.
        _, _, self.call_option_expiry, self.call_option_strike = parse_option_symbol(call_option_symbol)
        _, _, self.put_option_expiry, self.put_option_strike = parse_option_symbol(put_option_symbol)

        # --- Live Market State ---
        # These attributes hold the latest mid-price for each instrument.
        self.stock_price: float = 0.0
        self.call_option_price: float = 0.0
        self.put_option_price: float = 0.0

        # --- Triggering Logic State ---
        self.last_trigger_time: float = 0.0
        # Stores the stock price at the time of the last trigger to calculate the price change.
        self._last_checked_stock_price: float = 0.0
        # An exponential moving average of the stock's bid-ask spread, used for quote filtering.
        self._spread_ema: float = None

        # --- Options Pricing Parameters ---
        # On initialization, fetch the key inputs required for the QuantLib pricing model.
        days_to_expiry = (self.call_option_expiry - datetime.now().date()).days
        # Fetches the risk-free rate from the US Treasury yield curve for the given expiry.
        self.risk_free_rate = get_risk_free_rate(days_to_expiry)
        # Fetches the dividend yield for the underlying asset.
        self.dividend_yield = get_dividend_yield()

        # Instantiate the Alpaca data stream clients.
        self.option_stream = OptionDataStreamSigned(API_KEY, API_SECRET)
        self.stock_stream = StockDataStreamSigned(API_KEY, API_SECRET)

    async def _check_and_trigger(self):
        """
        Checks if the conditions for a delta recalculation are met and, if so,
        sends a trigger signal to the DeltaEngine.

        A trigger is sent if either of two conditions is true:
        1. The price of the underlying asset has moved more than PRICE_CHANGE_THRESHOLD.
        2. A certain amount of time (HEARTBEAT_TRIGGER_SECONDS) has passed since the
           last trigger, ensuring periodic recalculation even in a quiet market.
        """
        now = time.time()

        # Avoid triggering on the very first price update.
        if self._last_checked_stock_price == 0.0:
            if self.stock_price != 0.0:
                self._last_checked_stock_price = self.stock_price
            return

        price_change = abs(self.stock_price - self._last_checked_stock_price)

        # Condition 1: Price movement threshold is breached.
        if price_change >= PRICE_CHANGE_THRESHOLD:
            self._send_trigger(now)
            self._last_checked_stock_price = self.stock_price

        # Condition 2: Heartbeat interval is exceeded.
        elif now - self.last_trigger_time > HEARTBEAT_TRIGGER_SECONDS:
            logger.info("Heartbeat trigger: Forcing delta recalculation.")
            self._send_trigger(now)

    def _send_trigger(self, trigger_time: float):
        """
        Places a 'CALCULATE_DELTA' message onto the trigger queue.

        This method uses `put_nowait` to avoid blocking. If the queue is full
        (because the DeltaEngine is still working on a previous request), this
        call will simply be skipped. This is intentional, as it means the system
        will only ever process the most recent trigger. It also ensures all necessary
        market data has been received before sending a trigger.
        """
        if self.stock_price == 0.0 or self.call_option_price == 0.0 or self.put_option_price == 0.0:
            logger.warning("Skipping trigger: Market data is not yet complete.")
            return

        try:
            self.trigger_queue.put_nowait('CALCULATE_DELTA')
            self.last_trigger_time = trigger_time
        except asyncio.QueueFull:
            # It's okay to pass here. It just means the engine is busy,
            # and we're dropping this trigger in favor of a future one.
            pass

    async def _handle_stock_quote(self, quote):
        """
        Callback function for processing incoming stock quotes.

        It calculates the mid-price and applies a spread-based filter to ensure
        data quality before updating the official state and checking for triggers.
        An EMA of the spread is maintained to adapt to changing market conditions.
        """
        spread = quote.ask_price - quote.bid_price
        mid_price = (quote.bid_price + quote.ask_price) / 2

        # A quote is considered valid if its spread is not excessively wide
        # compared to the recent moving average of the spread.
        if mid_price > 0 and self._spread_ema is not None and spread <= 1.5 * self._spread_ema:
            self.stock_price = mid_price
            await self._check_and_trigger()

        # Update the spread EMA. We only use "good" quotes (spread < $0.50) to
        # prevent a single bad print from corrupting the EMA.
        if self._spread_ema is None and spread < 0.5:
            self._spread_ema = spread  # Initialize the EMA
        elif spread < 0.5:
            # Standard EMA formula
            self._spread_ema = 0.9 * self._spread_ema + 0.1 * spread

    async def _handle_option_quote(self, quote):
        """
        Callback function for processing incoming option quotes.

        Updates the respective option's price (call or put). No spread filter
        is applied here, as option spreads are naturally wider and more volatile.
        """
        mid_price = (quote.bid_price + quote.ask_price) / 2
        if mid_price > 0:
            if quote.symbol == self.call_option_symbol:
                self.call_option_price = mid_price
            elif quote.symbol == self.put_option_symbol:
                self.put_option_price = mid_price

    def subscribe_to_streams(self):
        """Subscribes the appropriate handler functions to the data streams."""
        self.option_stream.subscribe_quotes(self._handle_option_quote, self.call_option_symbol, self.put_option_symbol)
        self.stock_stream.subscribe_quotes(self._handle_stock_quote, HEDGING_ASSET)
        logger.info(f"Subscribed to stock ({HEDGING_ASSET}) and option ({self.call_option_symbol}, {self.put_option_symbol}) data streams.")

    async def run(self):
        """The main entry point for the MarketDataManager task."""
        self.subscribe_to_streams()
        logger.info("Market data manager running.")
        # The `_run_forever` methods are part of the Alpaca SDK's stream client.
        # They handle the long-running websocket connection. We run them
        # concurrently using asyncio.gather.
        await asyncio.gather(
            self.option_stream._run_forever(),
            self.stock_stream._run_forever(),
        )
