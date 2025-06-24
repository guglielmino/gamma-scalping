# delta_hedger/portfolio/position_manager.py

import asyncio
import time
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.stream import TradingStream
from config import API_KEY, API_SECRET, IS_PAPER_TRADING, HEDGING_ASSET, TRADE_COMMAND_TTL_SECONDS, INITIALIZATION_MODE

logger = logging.getLogger(__name__)

class PositionManager:
    """Manages portfolio state, executes trades, and handles fill confirmations."""
    def __init__(
        self,
        trade_action_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        self.trade_action_queue = trade_action_queue
        self.shutdown_event = shutdown_event
        self.trading_client = TradingClient(API_KEY, API_SECRET, paper=IS_PAPER_TRADING)
        self.trade_stream = TradingStream(API_KEY, API_SECRET, paper=IS_PAPER_TRADING)
        
        # --- Critical State ---
        self.shares_owned: int = 0
        self.pending_shares_change: int = 0

    async def initialize_position(self):
        """
        Initializes the position based on the configured mode.
        - 'resume': Starts with the existing position as is.
        - 'init': Clears all existing positions (stock and options) for a fresh start.
        """
        logger.info(f"Initializing in '{INITIALIZATION_MODE}' mode.")

        # Always cancel open orders for a clean slate, regardless of mode.
        try:
            self.trading_client.cancel_orders(symbol=HEDGING_ASSET)
            logger.info(f"Canceled any open orders for {HEDGING_ASSET}.")
        except Exception as e:
            logger.warning(f"Could not cancel open orders for {HEDGING_ASSET} (this is okay if there were none): {e}")


        if INITIALIZATION_MODE == 'init':
            await self._close_all_positions()
        elif INITIALIZATION_MODE == 'resume':
            await self._resume_position()


    async def _close_all_positions(self):
        """Finds and closes all positions related to the hedging asset (stocks and options)."""
        logger.info("Closing all positions for a fresh start...")
        try:
            # Close all positions, which includes both the underlying and any options.
            # Note: This is an aggressive action and will liquidate everything.
            # For a more targeted approach, you would list positions and close them individually.
            closed_positions = self.trading_client.close_all_positions(cancel_orders=True)
            for pos in closed_positions:
                logger.info(f"Closed position: {pos.symbol}, P/L: {pos.unrealized_pl}")
        except Exception as e:
            logger.error(f"Error while closing all positions: {e}")
        
        self.shares_owned = 0
        logger.info("All positions closed. Starting at 0 shares.")


    async def _resume_position(self):
        """Queries Alpaca for an existing position at startup."""
        try:
            position = self.trading_client.get_open_position(HEDGING_ASSET)
            self.shares_owned = int(float(position.qty))
            logger.info(f"Resumed position from Alpaca: {self.shares_owned} shares of {HEDGING_ASSET}.")
        except Exception: # alpaca.common.exceptions.APIError if no position
            self.shares_owned = 0
            logger.info(f"No existing position found for {HEDGING_ASSET}. Starting at 0 shares.")

    async def _handle_trade_fill(self, trade_update):
        """Processes a fill confirmation from the trade stream."""
        if trade_update.order.symbol == HEDGING_ASSET and trade_update.event in ('fill', 'partial_fill'):
            # --- LOGIC CORRECTED: We now maintain our own running calculation ---
            qty_this_fill = int(float(trade_update.qty))
            side = trade_update.order.side
            
            logger.info(
                f"Received fill: {side.value} {qty_this_fill} {HEDGING_ASSET}."
            )
            
            # Calculate the change in position from this specific fill.
            position_change_this_fill = qty_this_fill if side == OrderSide.BUY else -qty_this_fill
            
            # --- Reconcile state using a running calculation ---
            # Instead of trusting an external 'position_qty', we update our own state.
            self.shares_owned += position_change_this_fill
            
            # Decrement the pending shares by the amount that was just filled.
            self.pending_shares_change -= position_change_this_fill
            
            logger.info(
                f"State updated. New Confirmed Position: {self.shares_owned}, Pending: {self.pending_shares_change}"
            )

    async def trade_executor_loop(self):
        """Consumes trade commands from the queue and executes them."""
        logger.info("Trade Executor started. Waiting for commands...")
        while not self.shutdown_event.is_set():
            try:
                command = await asyncio.wait_for(self.trade_action_queue.get(), timeout=1.0)
                
                # --- TTL Check ---
                command_age = time.time() - command.get("timestamp", 0)
                if command_age > TRADE_COMMAND_TTL_SECONDS:
                    logger.warning(f"Discarding STALE trade command. Age: {command_age:.2f}s")
                    self.trade_action_queue.task_done()
                    continue

                # --- Execute Trade ---
                qty = command['quantity']
                side = OrderSide.BUY if qty > 0 else OrderSide.SELL
                
                logger.info(f"Executing trade command: {side.value} {abs(qty)} {HEDGING_ASSET}")
                
                # Update pending state immediately
                self.pending_shares_change += qty

                # Prepare and submit the order
                market_order_data = MarketOrderRequest(
                    symbol=HEDGING_ASSET,
                    qty=abs(qty),
                    side=side,
                    time_in_force=TimeInForce.DAY
                )
                self.trading_client.submit_order(order_data=market_order_data)
                
                self.trade_action_queue.task_done()

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error in trade executor: {e}")

    async def fill_listener_loop(self):
        """Listens to the trading stream for fill events."""
        logger.info("Fill Listener started.")
        # The SDK handles the reconnect logic internally.
        self.trade_stream.subscribe_trade_updates(self._handle_trade_fill)
        await self.trade_stream._run_forever()

