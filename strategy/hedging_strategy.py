# delta_hedger/strategy/hedging_strategy.py

import asyncio
import time
import logging
from portfolio.position_manager import PositionManager
from config import HEDGING_DELTA_THRESHOLD, OPTIONS_CONTRACT_MULTIPLIER, STRATEGY_MULTIPLIER

logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    The "brain" of the operation. Consumes deltas, reads portfolio state,
    and decides when to issue a hedging trade command.
    """
    def __init__(
        self,
        position_manager: PositionManager,
        delta_queue: asyncio.Queue,
        trade_action_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        self.position_manager = position_manager
        self.delta_queue = delta_queue
        self.trade_action_queue = trade_action_queue
        self.shutdown_event = shutdown_event
        
    async def run(self):
        logger.info("Trading Strategy started. Waiting for delta updates...")
        while not self.shutdown_event.is_set():
            try:
                options_delta_per_share = await asyncio.wait_for(self.delta_queue.get(), timeout=1.0)
                
                # --- Core Hedging Logic ---
                # Scale the per-share delta by the contract size and the number of straddles.
                total_options_delta = options_delta_per_share * OPTIONS_CONTRACT_MULTIPLIER * STRATEGY_MULTIPLIER
                
                # Read current position state, including shares being traded
                current_position = self.position_manager.shares_owned
                pending_position = self.position_manager.pending_shares_change
                total_hedge_delta = current_position + pending_position

                # Calculate net risk
                net_delta_per_share = (total_options_delta + total_hedge_delta) / STRATEGY_MULTIPLIER
                
                logger.info(f"New Greeks received. Options Δ: {total_options_delta:+.2f}, Hedge Δ: {total_hedge_delta}, Net Δ Per Share: {net_delta_per_share:+.2f}")

                # Apply the "dead band" logic, scaling the threshold by the strategy size.
                if abs(net_delta_per_share) > HEDGING_DELTA_THRESHOLD:
                    # Calculate trade needed to return to zero
                    shares_to_trade = -round(net_delta_per_share * STRATEGY_MULTIPLIER)
                    
                    logger.warning(f"Hedge threshold breached! Net Delta is {net_delta_per_share:+.2f}. Issuing command to trade {shares_to_trade} shares.")

                    # Create and send the time-stamped command
                    await self._queue_trade_command(shares_to_trade)

                self.delta_queue.task_done()

            except asyncio.TimeoutError:
                continue

        logger.info("Trading Strategy shutting down.")

    async def _queue_trade_command(self, quantity: int):
        """
        Creates a trade command and puts it on the queue.
        Clears any existing command to ensure only the latest is processed.
        """
        # Clear the queue of any stale commands.
        if not self.trade_action_queue.empty():
            try:
                self.trade_action_queue.get_nowait()
                self.trade_action_queue.task_done()
            except asyncio.QueueEmpty:
                pass # It's okay if it was emptied by another task.

        command = {
            "type": "TRADE",
            "quantity": quantity,
            "timestamp": time.time()
        }
        await self.trade_action_queue.put(command)

