# delta_hedger/market/state.py

import asyncio
import time
import logging
from alpaca.data.live import OptionDataStream, StockDataStream
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    PRICE_CHANGE_THRESHOLD, HEARTBEAT_TRIGGER_SECONDS
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

class MarketState:
    """Holds the latest market data and manages the trigger logic."""
    def __init__(self, trigger_queue: asyncio.Queue, call_option_symbol: str, put_option_symbol: str):
        self.trigger_queue = trigger_queue

        # State attributes
        self.stock_price: float = 0.0
        self.call_option_price: float = 0.0
        self.put_option_price: float = 0.0
        self.last_trigger_time: float = 0.0
        self.call_option_symbol = call_option_symbol
        self.put_option_symbol = put_option_symbol
        
        # Used for threshold checking
        self._last_checked_stock_price: float = 0.0


    async def _check_and_trigger(self):
        """Internal method to check conditions and send a trigger."""
        now = time.time()
        price_change = abs(self.stock_price - self._last_checked_stock_price)

        # Condition 1: Price moved significantly
        if price_change > PRICE_CHANGE_THRESHOLD:
            logger.info(f"Threshold breach. Price change: {price_change:.2f}")
            self._send_trigger(now)
            self._last_checked_stock_price = self.stock_price
            
        # Condition 2: Heartbeat
        elif now - self.last_trigger_time > HEARTBEAT_TRIGGER_SECONDS:
            logger.info("Heartbeat trigger.")
            self._send_trigger(now)
            self._last_checked_stock_price = self.stock_price

    def _send_trigger(self, trigger_time: float):
        """Puts a trigger on the queue if it's not already full."""
        try:
            self.trigger_queue.put_nowait('CALCULATE_DELTA')
            self.last_trigger_time = trigger_time
        except asyncio.QueueFull:
            # This is okay, means the engine is already busy.
            pass

    # --- Alpaca Stream Handlers ---
    async def handle_stock_quote(self, quote):
        self.stock_price = quote.bid_price # Using bid for simplicity
        logger.debug(f"Received stock quote: {self.stock_price}")
        await self._check_and_trigger()

    async def handle_option_quote(self, quote):
        mid_price = (quote.bid_price + quote.ask_price) / 2
        if quote.symbol == self.call_option_symbol:
            self.call_option_price = mid_price
        elif quote.symbol == self.put_option_symbol:
            self.put_option_price = mid_price
        logger.debug(f"Received option quote for {quote.symbol}: {mid_price}")
        # We primarily trigger off the underlying's movement, but could add logic here too.


class MarketDataStreamer:
    """Manages the connection to the Alpaca data streams."""
    def __init__(self, market_state: MarketState):
        self.market_state = market_state
        self.option_stream = OptionDataStream(API_KEY, API_SECRET)
        self.stock_stream = StockDataStream(API_KEY, API_SECRET)

    def subscribe_to_streams(self):
        self.option_stream.subscribe_quotes(self.market_state.handle_option_quote, self.market_state.call_option_symbol, self.market_state.put_option_symbol)
        self.stock_stream.subscribe_quotes(self.market_state.handle_stock_quote, HEDGING_ASSET)
        logger.info("Subscribed to stock and option data streams.")

    async def run(self):
        """Runs the data streams concurrently."""
        await asyncio.gather(
            self.option_stream._run_forever(),
            self.stock_stream._run_forever(),
        )
