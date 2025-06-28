# delta_hedger/strategy/hedging_strategy.py

import asyncio
import time
import logging
from portfolio.position_manager import PositionManager
from config import HEDGING_DELTA_THRESHOLD, OPTIONS_CONTRACT_MULTIPLIER, STRATEGY_MULTIPLIER

# Configure logging for this module
logger = logging.getLogger(__name__)


class TradingStrategy:
    """
    The "brain" of the gamma scalping operation.

    This class acts as the central decision-making component. It runs in a
    continuous loop, performing the following functions:
    1.  Listens for new portfolio delta calculations from the `delta_queue`,
        which are produced by the DeltaEngine.
    2.  Reads the current position state (shares owned and pending) from the
        PositionManager.
    3.  Calculates the net, overall delta of the entire portfolio (options + stock).
    4.  Compares this net delta against a predefined threshold (the "dead band").
    5.  If the threshold is breached, it calculates the required hedge trade size
        and places a trade command on the `trade_action_queue` to be executed
        by the PositionManager.
    """
    def __init__(
        self,
        position_manager: PositionManager,
        delta_queue: asyncio.Queue,
        trade_action_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        """
        Initializes the TradingStrategy.

        Args:
            position_manager: A reference to the PositionManager to get live position data.
            delta_queue: The input queue for receiving delta calculations from the DeltaEngine.
            trade_action_queue: The output queue for sending trade commands to the PositionManager.
            shutdown_event: The event to signal graceful shutdown.
        """
        self.position_manager = position_manager
        self.delta_queue = delta_queue
        self.trade_action_queue = trade_action_queue
        self.shutdown_event = shutdown_event
        
    async def run(self):
        """The main loop for the Trading Strategy task."""
        logger.info("Trading Strategy started. Waiting for delta updates...")
        while not self.shutdown_event.is_set():
            try:
                # Wait for a new delta calculation from the DeltaEngine.
                # A timeout is used to allow the shutdown event to be checked periodically.
                options_delta_per_share = await asyncio.wait_for(self.delta_queue.get(), timeout=1.0)
                
                # --- Core Hedging Logic ---

                # 1. Calculate the total delta contributed by our options positions.
                # This is the per-share delta of the straddle, scaled by the number of shares
                # per contract and the number of straddles we are trading.
                total_options_delta = options_delta_per_share * OPTIONS_CONTRACT_MULTIPLIER * STRATEGY_MULTIPLIER
                
                # 2. Get the delta of our hedge (the stock position).
                # It's crucial to include both shares currently owned and any shares
                # from pending trades that have not yet been filled. This prevents
                # sending duplicate orders while a hedge is in flight.
                current_position = self.position_manager.shares_owned
                pending_position = self.position_manager.pending_shares_change
                total_hedge_delta = current_position + pending_position

                # 3. Calculate the net portfolio delta.
                # This is the sum of the options delta and the hedge delta.
                # We normalize it by the strategy multiplier to get a "per-straddle"
                # delta, which makes the threshold easier to reason about.
                net_delta_per_share = (total_options_delta + total_hedge_delta) / STRATEGY_MULTIPLIER
                
                logger.info(
                    f"New delta received. Options Δ: {total_options_delta:+.2f}, "
                    f"Hedge Δ: {total_hedge_delta:+.2f}, Net Δ Per Share: {net_delta_per_share:+.2f}"
                )

                # 4. Check if the net delta has breached our hedging threshold.
                # This "dead band" is the core of the strategy. If the delta is within
                # this band, we do nothing to avoid costs from over-trading.
                if abs(net_delta_per_share) > HEDGING_DELTA_THRESHOLD:
                    # The portfolio is insufficiently hedged. Calculate the trade size
                    # needed to bring the net delta back to zero.
                    shares_to_trade = -round(net_delta_per_share * STRATEGY_MULTIPLIER)
                    
                    if shares_to_trade == 0:
                        logger.debug("Threshold breached, but required hedge rounded to 0 shares. No action taken.")
                        continue

                    logger.warning(
                        f"Hedge threshold breached! Delta is {net_delta_per_share:+.2f}. "
                        f"Issuing command to trade {shares_to_trade} shares."
                    )

                    # 5. Create and send the trade command to the PositionManager.
                    await self._queue_trade_command(shares_to_trade)

            except asyncio.TimeoutError:
                # This is expected when no new delta arrives within the timeout period.
                continue
            finally:
                # Acknowledge that the delta message has been processed.
                if not self.delta_queue.empty():
                    self.delta_queue.task_done()

        logger.info("Trading Strategy shutting down.")

    async def _queue_trade_command(self, quantity: int):
        """
        Creates a trade command and puts it on the trade_action_queue.

        It ensures that any old, unprocessed command is cleared from the queue
        first. This is critical because if the delta changes again quickly, we want
        the PositionManager to act on the newest hedge calculation, not a stale one.
        """
        # Clear the queue of any stale commands.
        if not self.trade_action_queue.empty():
            try:
                self.trade_action_queue.get_nowait()
                self.trade_action_queue.task_done()
            except asyncio.QueueEmpty:
                pass  # It's okay if it was emptied by another task.

        # The command is a dictionary containing all necessary info for the trade.
        # A timestamp is included for potential TTL (Time-To-Live) checks.
        command = {
            "type": "TRADE",
            "quantity": quantity,
            "timestamp": time.time()
        }
        await self.trade_action_queue.put(command)

