# delta_hedger/portfolio/position_manager.py

import asyncio
import time
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass
from alpaca.trading.stream import TradingStream
from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, HEDGING_ASSET, 
    TRADE_COMMAND_TTL_SECONDS, INITIALIZATION_MODE, STRATEGY_MULTIPLIER
)
from utils.parsing import parse_option_symbol

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
        self.call_option_symbol: str | None = None
        self.put_option_symbol: str | None = None

        self._trade_lock = asyncio.Event()
        self._trade_lock.set()
        
        # Stores the second leg of a two-part trade (e.g., when going from long to short)
        self._pending_second_leg = None

    async def initialize_position(self):
        """
        Initializes the position based on the configured mode.
        - 'resume': Starts with the existing position as is.
        - 'init': Clears all existing positions (stock and options) for a fresh start.
        """
        logger.info(f"Initializing in '{INITIALIZATION_MODE}' mode.")

        # Always cancel open orders for a clean slate, regardless of mode.
        try:
            self.trading_client.cancel_orders()
            logger.info(f"Canceled any open orders.")
        except Exception as e:
            logger.warning(f"Could not cancel open orders: {e}")


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
            positions = self.trading_client.get_all_positions()
            closed_positions = []
            for pos in positions:
                if pos.asset_class == AssetClass.US_OPTION:
                    underlying, _, _, _ = parse_option_symbol(pos.symbol)
                    if underlying == HEDGING_ASSET:
                        self.trading_client.close_position(pos.symbol)
                        closed_positions.append(pos.symbol)
                elif pos.asset_class == AssetClass.US_EQUITY:
                    if pos.symbol == HEDGING_ASSET:
                        self.trading_client.close_position(pos.symbol)
                        closed_positions.append(pos.symbol) 

            logger.info(f"Closed positions: {closed_positions}")
        except Exception as e:
            logger.error(f"Error while closing all positions: {e}")
        
        self.shares_owned = 0
        logger.info("All positions closed. Starting at 0 shares.")


    async def _resume_position(self):
        """
        Queries Alpaca for existing positions at startup.
        Identifies stock position and the single call/put option pair for the hedging asset.
        """
        logger.info(f"Resuming positions for {HEDGING_ASSET}...")
        self.shares_owned = 0

        try:
            positions = self.trading_client.get_all_positions()

            # First, find the stock position for the hedging asset
            for pos in positions:
                if pos.asset_class == AssetClass.US_EQUITY and pos.symbol == HEDGING_ASSET:
                    self.shares_owned = int(float(pos.qty))
                    logger.info(f"Resumed stock position: {self.shares_owned} shares of {HEDGING_ASSET}.")
                    break  # Found it, no need to continue looping for this

            # Now, find the options positions
            option_positions = []
            for pos in positions:
                if pos.asset_class == AssetClass.US_OPTION:
                    underlying, option_type, _, _ = parse_option_symbol(pos.symbol)
                    if underlying == HEDGING_ASSET:
                        option_positions.append((pos.symbol, option_type, int(float(pos.qty))))

            if not option_positions:
                raise ValueError("No option positions found for the hedging asset. Must have one call and one put.")

            # Check for exactly one call and one put, with quantities matching the multiplier
            calls = [(p, q) for p, t, q in option_positions if t == 'C']
            puts = [(p, q) for p, t, q in option_positions if t == 'P']

            if len(calls) != 1 or len(puts) != 1:
                raise ValueError(
                    f"Expected exactly one call and one put for {HEDGING_ASSET}, "
                    f"but found {len(calls)} calls and {len(puts)} puts."
                )

            call_symbol, call_qty = calls[0]
            put_symbol, put_qty = puts[0]

            if call_qty != put_qty:
                raise ValueError(
                    f"Mismatched option quantities. Call qty: {call_qty}, Put qty: {put_qty}."
                )
            
            if call_qty != STRATEGY_MULTIPLIER:
                raise ValueError(
                    f"Position quantity ({call_qty}) does not match STRATEGY_MULTIPLIER ({STRATEGY_MULTIPLIER})."
                )

            self.call_option_symbol = call_symbol
            self.put_option_symbol = put_symbol

            logger.info(f"Resumed option positions: Call={self.call_option_symbol}, Put={self.put_option_symbol}, Qty={call_qty}")

        except Exception as e:
            logger.error(f"Error resuming positions: {e}")
            # In case of any error (including no positions found), we start fresh
            self.shares_owned = 0
            self.call_option_symbol = None
            self.put_option_symbol = None
            logger.info("Could not resume positions.")
            raise e  # Re-raise the exception to halt initialization if resume fails validation

    async def _handle_trade_fill(self, data):
        """The callback handler for trade update events."""
        logger.info(f"Trade update received: {data.event}")
        
        if data.event in ['fill', 'partial_fill', 'canceled', 'rejected']:
            if data.event == 'partial_fill':
                logger.warning("Partial fill received. Position state may be temporarily inconsistent.")
                return

            order_qty = int(data.order.filled_qty)
            side_multiplier = 1 if data.order.side == 'buy' else -1
            
            self.shares_owned += (order_qty * side_multiplier)
            self.pending_shares_change -= (order_qty * side_multiplier)
            
            logger.warning(f"Order {data.event}. Position is now {self.shares_owned} shares.")

            if self._pending_second_leg:
                logger.info("Executing second leg of two-part trade.")
                leg2 = self._pending_second_leg
                self._pending_second_leg = None
                await self._execute_trade(leg2['quantity'], leg2['side'])
            else:
                logger.info("Order is terminal. Releasing trade lock.")
                self._trade_lock.set()

    async def _execute_trade(self, quantity: int, side: OrderSide):
        """Submits a single trade and handles immediate errors."""
        try:
            order_request = MarketOrderRequest(
                symbol=HEDGING_ASSET,
                qty=abs(quantity),
                side=side,
                time_in_force=TimeInForce.DAY
            )
            self.trading_client.submit_order(order_data=order_request)
            self.pending_shares_change += quantity
            logger.info(f"Submitted market order to {side.value} {abs(quantity)} {HEDGING_ASSET}. Pending change: {self.pending_shares_change}")
        except Exception as e:
            logger.error(f"Error submitting order: {e}")
            self._trade_lock.set() # Release lock on failure

    async def trade_executor_loop(self):
        logger.info("Trade executor started.")
        while not self.shutdown_event.is_set():
            await self._trade_lock.wait()
            
            command = await self.trade_action_queue.get()
            self._trade_lock.clear()

            if time.time() - command["timestamp"] > TRADE_COMMAND_TTL_SECONDS:
                logger.warning(f"Discarding stale trade command: {command}")
                self.trade_action_queue.task_done()
                self._trade_lock.set()
                continue

            quantity = command['quantity']
            side = OrderSide.BUY if quantity > 0 else OrderSide.SELL

            # --- Long/Short Transition Logic ---
            if side == OrderSide.SELL and self.shares_owned > 0 and abs(quantity) > self.shares_owned:
                # Sell to flatten position, then sell to open short
                qty1 = -self.shares_owned
                qty2 = quantity - qty1
                self._pending_second_leg = {'quantity': qty2, 'side': OrderSide.SELL}
                logger.warning(f"Trade crosses zero. Leg 1: Sell {abs(qty1)}. Pending Leg 2: Sell {abs(qty2)}.")
                await self._execute_trade(qty1, OrderSide.SELL)
            elif side == OrderSide.BUY and self.shares_owned < 0 and quantity > abs(self.shares_owned):
                # Buy to cover short, then buy to open long
                qty1 = abs(self.shares_owned)
                qty2 = quantity - qty1
                self._pending_second_leg = {'quantity': qty2, 'side': OrderSide.BUY}
                logger.warning(f"Trade crosses zero. Leg 1: Buy {qty1}. Pending Leg 2: Buy {qty2}.")
                await self._execute_trade(qty1, OrderSide.BUY)
            else:
                # Standard trade, no splitting required
                await self._execute_trade(quantity, side)
            
            self.trade_action_queue.task_done()

    async def fill_listener_loop(self):
        """Listens to the trading stream for fill events."""
        logger.info("Fill Listener started.")
        self.trade_stream.subscribe_trade_updates(self._handle_trade_fill)
        # The SDK handles the reconnect logic internally.
        # We call _run_forever() because run() is a blocking call that starts its own event loop.
        await self.trade_stream._run_forever()

