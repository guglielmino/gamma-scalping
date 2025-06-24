# delta_hedger/engine/delta_engine.py

import asyncio
import random
import time
import logging
import QuantLib as ql
from datetime import datetime
from typing import Tuple
from market.state import MarketDataManager

logger = logging.getLogger(__name__) 

def calculate_implied_volatility(
    option_market_price: float,
    stock_price: float,
    strike: float,
    expiry_days: int,
    option_type: str,  # 'call' or 'put'
    risk_free_rate: float,
    dividend_yield: float,
    n_steps: int = 100
) -> float:
    """Calculates the implied volatility for an American option using a binomial tree."""
    ql.Settings.instance().evaluationDate = ql.Date.todaysDate()

    # print(option_market_price, stock_price, strike, expiry_days, option_type, risk_free_rate, dividend_yield, n_steps)
    
    if option_type.lower() == 'call':
        payoff = ql.PlainVanillaPayoff(ql.Option.Call, strike)
    else:
        payoff = ql.PlainVanillaPayoff(ql.Option.Put, strike)
        
    exercise = ql.AmericanExercise(ql.Date.todaysDate(), ql.Date.todaysDate() + expiry_days)
    option = ql.VanillaOption(payoff, exercise)
    
    spot_handle = ql.QuoteHandle(ql.SimpleQuote(stock_price))
    riskfree_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), risk_free_rate, ql.Actual365Fixed()))
    dividend_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), dividend_yield, ql.Actual365Fixed()))
    
    # We need a dummy volatility to create the process, but it will be ignored by the solver.
    volatility_handle = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(0, ql.TARGET(), 0.20, ql.Actual365Fixed()))
    bsm_process = ql.BlackScholesMertonProcess(spot_handle, dividend_handle, riskfree_handle, volatility_handle)
    
    engine = ql.BinomialVanillaEngine(bsm_process, "crr", n_steps)
    option.setPricingEngine(engine)
    
    # Accuracy of the solver
    accuracy = 0.0001
    max_iterations = 100
    min_vol, max_vol = 0.01, 4.0 # Search range for volatility
    
    try:
        iv = option.impliedVolatility(option_market_price, bsm_process, accuracy, max_iterations, min_vol, max_vol)
        logger.debug(f"Implied volatility calculation successful: {iv:.4f} using {n_steps} steps.")
        return iv
    except RuntimeError as e:
        logger.error(f"Failed to calculate implied volatility for {option_type} K={strike}: {e}")
        # Return a sensible default or NaN if calculation fails
        return float('nan')

def calculate_single_option_greeks(
    option_market_price: float,
    stock_price: float,
    strike: float,
    expiry_days: int,
    option_type: str,  # 'call' or 'put'
    risk_free_rate: float,
    dividend_yield: float,
    requested_greeks: list[str],
    iv_steps: int = 100,
    greeks_steps: int = 100
) -> dict[str, float]:
    """
    Calculates the requested Greeks for a single American option.
    It first calculates implied volatility from the market price.
    """
    
    # Step 1: Calculate Implied Volatility using the "fast" engine
    implied_vol = calculate_implied_volatility(
        option_market_price, stock_price, strike, expiry_days,
        option_type, risk_free_rate, dividend_yield, n_steps=iv_steps
    )
    
    if implied_vol is None or implied_vol != implied_vol: # Check for NaN
        logger.warning(f"Skipping Greeks calculation due to failed IV calculation.")
        return {greek: float('nan') for greek in requested_greeks}

    # Step 2: Calculate Greeks using the "high-precision" engine
    ql.Settings.instance().evaluationDate = ql.Date.todaysDate()

    if option_type.lower() == 'call':
        payoff = ql.PlainVanillaPayoff(ql.Option.Call, strike)
    else:
        payoff = ql.PlainVanillaPayoff(ql.Option.Put, strike)
        
    exercise = ql.AmericanExercise(ql.Date.todaysDate(), ql.Date.todaysDate() + expiry_days)
    option = ql.VanillaOption(payoff, exercise)
    
    spot_handle = ql.QuoteHandle(ql.SimpleQuote(stock_price))
    riskfree_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), risk_free_rate, ql.Actual365Fixed()))
    dividend_handle = ql.YieldTermStructureHandle(ql.FlatForward(0, ql.TARGET(), dividend_yield, ql.Actual365Fixed()))
    
    # Use the calculated implied volatility for this process
    volatility_handle = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(0, ql.TARGET(), implied_vol, ql.Actual365Fixed()))
    bsm_process = ql.BlackScholesMertonProcess(spot_handle, dividend_handle, riskfree_handle, volatility_handle)
    
    engine = ql.BinomialVanillaEngine(bsm_process, "crr", greeks_steps)
    option.setPricingEngine(engine)
    
    greeks = {}
    if 'delta' in requested_greeks:
        greeks['delta'] = option.delta()
    if 'theta' in requested_greeks:
        greeks['theta'] = option.theta() / 365.0
    if 'gamma' in requested_greeks:
        greeks['gamma'] = option.gamma()
    
    log_msg_parts = [f"{k.capitalize()}={v:.6f}" for k, v in greeks.items()]
    logger.debug(f"Greeks calculated with {greeks_steps} steps: {', '.join(log_msg_parts)}")
    
    return greeks



class DeltaEngine:
    """
    Listens for triggers, fetches the latest market data from the MarketDataManager,
    calculates portfolio options delta, and pushes it to a queue.
    """
    def __init__(
        self,
        market_manager: MarketDataManager,
        trigger_queue: asyncio.Queue,
        delta_queue: asyncio.Queue,
        shutdown_event: asyncio.Event
    ):
        self.market_manager = market_manager
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
            # Wait for a trigger from the MarketDataManager
            try:
                await asyncio.wait_for(self.trigger_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            # Active cycle: process triggers until the queue is empty
            while True:
                stock = self.market_manager.stock_price
                call = self.market_manager.call_option_price
                put = self.market_manager.put_option_price
                print(stock, call, put)

                # Offload the blocking, CPU-intensive work to a separate thread.
                put_greeks = await asyncio.to_thread(
                    calculate_single_option_greeks,
                    put,
                    stock,
                    self.market_manager.put_option_strike,
                    (self.market_manager.put_option_expiry - datetime.now().date()).days,
                    "put",
                    self.market_manager.risk_free_rate,
                    self.market_manager.dividend_yield,
                    ['delta']
                )

                call_greeks = await asyncio.to_thread(
                    calculate_single_option_greeks,
                    call,
                    stock,
                    self.market_manager.call_option_strike,
                    (self.market_manager.call_option_expiry - datetime.now().date()).days,
                    "call",
                    self.market_manager.risk_free_rate,
                    self.market_manager.dividend_yield,
                    ['delta']
                )

                delta = put_greeks["delta"] + call_greeks["delta"]
                
                await self._publish_result(delta)
                self.trigger_queue.task_done()

                try:
                    self.trigger_queue.get_nowait()
                except asyncio.QueueEmpty:
                    logger.debug("Delta trigger queue empty. Returning to idle.")
                    break
        
        logger.info("Delta Engine shutting down.")


