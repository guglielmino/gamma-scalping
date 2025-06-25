# delta_hedger/market/state.py

import asyncio
import time
import logging
from alpaca.data.live import OptionDataStream, StockDataStream
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    PRICE_CHANGE_THRESHOLD, HEARTBEAT_TRIGGER_SECONDS
)
from utils.parsing import parse_option_symbol

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

class MarketDataManager:
    """
    Manages the connection to Alpaca data streams, holds the latest market data,
    and manages the trigger logic for downstream calculations.
    """
    def __init__(self, trigger_queue: asyncio.Queue, call_option_symbol: str, put_option_symbol: str):
        self.trigger_queue = trigger_queue
        self.call_option_symbol = call_option_symbol
        self.put_option_symbol = put_option_symbol
        _, _, self.call_option_expiry, self.call_option_strike = parse_option_symbol(call_option_symbol)
        _, _, self.put_option_expiry, self.put_option_strike = parse_option_symbol(put_option_symbol)

        # State attributes initialized to zero
        self.stock_price: float = 0.0
        self.call_option_price: float = 0.0
        self.put_option_price: float = 0.0
        self.last_trigger_time: float = 0.0
        
        # Used for threshold checking
        self._last_checked_stock_price: float = 0.0
        self._spread_ema: float = None

        # Temporarily hardcode the risk free rate and dividend yield
        self.risk_free_rate = 0.05
        self.dividend_yield = 0.0
        
        # Alpaca stream clients
        self.option_stream = OptionDataStream(API_KEY, API_SECRET)
        self.stock_stream = StockDataStream(API_KEY, API_SECRET)

    async def _check_and_trigger(self):
        """Internal method to check conditions and send a trigger."""
        now = time.time()
        
        # Wait until we have an initial price to avoid triggering on startup
        if self._last_checked_stock_price == 0.0:
            if self.stock_price != 0.0:
                self._last_checked_stock_price = self.stock_price
            return

        price_change = abs(self.stock_price - self._last_checked_stock_price)

        if price_change >= PRICE_CHANGE_THRESHOLD:
            # logger.info(f"Threshold breach. Price change: {price_change:.2f}. Old: {self._last_checked_stock_price}, New: {self.stock_price}")
            self._send_trigger(now)
            self._last_checked_stock_price = self.stock_price
            
        elif now - self.last_trigger_time > HEARTBEAT_TRIGGER_SECONDS:
            logger.info("Heartbeat trigger.")
            self._send_trigger(now)

    def _send_trigger(self, trigger_time: float):
        """Puts a trigger on the queue if it's not already full."""
        if self.stock_price == 0.0 or self.call_option_price == 0.0 or self.put_option_price == 0.0:
            logger.warning("Skipping trigger. Market data is not yet complete.")
            return
        
        try:
            self.trigger_queue.put_nowait('CALCULATE_DELTA')
            self.last_trigger_time = trigger_time
        except asyncio.QueueFull:
            pass

    async def _handle_stock_quote(self, quote):
        spread = quote.ask_price - quote.bid_price
        mid_price = (quote.bid_price + quote.ask_price) / 2
        # Filter out bad quotes
        # print(f"spread: {spread}, ema: {self._spread_ema}")
        if mid_price > 0 and self._spread_ema is not None and spread <= 1.5 * self._spread_ema:
            self.stock_price = mid_price
            await self._check_and_trigger()
        if self._spread_ema is None and spread < 0.5:
            self._spread_ema = spread
        elif spread < 0.5:
            self._spread_ema = 0.9 * self._spread_ema + 0.1 * spread

    async def _handle_option_quote(self, quote):
        mid_price = (quote.bid_price + quote.ask_price) / 2
        if mid_price > 0:
            if quote.symbol == self.call_option_symbol:
                self.call_option_price = mid_price
            elif quote.symbol == self.put_option_symbol:
                self.put_option_price = mid_price

    def subscribe_to_streams(self):
        self.option_stream.subscribe_quotes(self._handle_option_quote, self.call_option_symbol, self.put_option_symbol)
        self.stock_stream.subscribe_quotes(self._handle_stock_quote, HEDGING_ASSET)
        logger.info("Subscribed to stock and option data streams.")

    async def run(self):
        """Runs the data streams concurrently."""
        self.subscribe_to_streams()
        logger.info("Market data manager running.")
        await asyncio.gather(
            self.option_stream._run_forever(),
            self.stock_stream._run_forever(),
        )
