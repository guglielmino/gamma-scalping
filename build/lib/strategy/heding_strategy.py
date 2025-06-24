# delta_hedger/strategy/hedging_strategy.py

import asyncio
import time
import logging
from portfolio.position_manager import PositionManager
from config import HEDGING_DELTA_THRESHOLD

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
                options_delta = await asyncio.wait_for(self.delta_queue.get(), timeout=1.0)

                # Read current position state
                current_position = self.position_manager.shares_owned
                pending_position = self.position_manager.pending_shares_change
                total_hedge_delta = current_position + pending_position

                # Calculate net risk
                net_delta = options_delta + total_hedge_delta
                
                logger.info(f"New Delta received. Options Δ: {options_delta:+.2f}, Hedge Δ: {total_hedge_delta}, Net Δ: {net_delta:+.2f}")

                # Apply the "dead band" logic
                if abs(net_delta) > HEDGING_DELTA_THRESHOLD:
                    # Calculate trade needed to return to zero
                    shares_to_trade = -round(net_delta)
                    
                    logger.warning(f"Hedge threshold breached! Net Delta is {net_delta:+.2f}. Issuing command to trade {shares_to_trade} shares.")

                    # Create and send the time-stamped command
                    command = {
                        "quantity": shares_to_trade,
                        "timestamp": time.time()
                    }
                    await self.trade_action_queue.put(command)

                self.delta_queue.task_done()

            except asyncio.TimeoutError:
                continue

        logger.info("Trading Strategy shutting down.")

