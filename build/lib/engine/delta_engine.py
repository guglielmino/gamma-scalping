# delta_hedger/engine/delta_engine.py

import asyncio
import random
import time
import logging
from market.state import MarketState

logger = logging.getLogger(__name__)

def calculate_delta_cpu_bound(stock_price: float, call_price: float, put_price: float) -> float:
    """
    Placeholder for the real, CPU-intensive options delta calculation.
    In a real system, this function would take more parameters (strike, time, vol)
    and use a model like binomial tree to calculate the delta for the call and put.
    """
    logger.debug(f"Starting delta calculation for S={stock_price}, C={call_price}, P={put_price}")
    # Simulate heavy work
    time.sleep(0.5)
    # Mock calculation: for this example, we'll just return a random-ish value
    # to simulate a changing delta. A real model is needed here.
    mock_call_delta = 0.5 + (stock_price - 100) * 0.01
    mock_put_delta = -0.5 + (stock_price - 100) * 0.01
    total_options_delta = (mock_call_delta + mock_put_delta) * 100 # Options are for 100 shares
    logger.debug(f"Finished delta calculation. Result: {total_options_delta}")
    return total_options_delta


class DeltaEngine:
    """Calculates portfolio options delta and pushes it to a queue."""
    def __init__(
        self,
        market_state: MarketState,
        trigger_queue: asyncio.Queue,
        delta_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        self.market_state = market_state
        self.trigger_queue = trigger_queue
        self.delta_queue = delta_queue
        self.shutdown_event = shutdown_event

    async def _publish_result(self, delta: float):
        """Puts the calculated delta into the output queue, overwriting any stale data."""
        try:
            # Clear any old delta value that the strategy hasn't consumed yet.
            if not self.delta_queue.empty():
                self.delta_queue.get_nowait()
                self.delta_queue.task_done()
            self.delta_queue.put_nowait(delta)
        except asyncio.QueueFull:
            pass # Safe to ignore, another task will likely add a fresher value.

    async def run(self):
        logger.info("Delta Engine started. Waiting for triggers...")
        while not self.shutdown_event.is_set():
            try:
                await asyncio.wait_for(self.trigger_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Active cycle: process triggers until the queue is empty
            while True:
                stock = self.market_state.stock_price
                call = self.market_state.call_option_price
                put = self.market_state.put_option_price

                # Offload the blocking, CPU-intensive work to a separate thread.
                options_delta = await asyncio.to_thread(calculate_delta_cpu_bound, stock, call, put)
                
                await self._publish_result(options_delta)
                self.trigger_queue.task_done()

                try:
                    self.trigger_queue.get_nowait()
                except asyncio.QueueEmpty:
                    logger.debug("Delta trigger queue empty. Returning to idle.")
                    break
        
        logger.info("Delta Engine shutting down.")
