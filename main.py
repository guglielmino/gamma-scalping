import asyncio
import logging
import config

# Import all our component classes
from market.state import MarketDataManager
from engine.delta_engine import DeltaEngine
from portfolio.position_manager import PositionManager
from strategy.hedging_strategy import TradingStrategy
from strategy.options_strategy import open_initial_straddle

# Configure logging
logging.basicConfig(level=config.LOG_LEVEL, format=config.LOG_FORMAT)
logger = logging.getLogger(__name__)

async def main():
    """
    Main orchestration function.
    Initializes all components, injects dependencies, and runs them concurrently.
    """
    shutdown_event = asyncio.Event()
    # Used to send trade commands from the Strategy to the Position Manager.
    # Max size of 1 ensures the latest command overwrites any prior one.
    trade_action_queue = asyncio.Queue(maxsize=1)

    position_manager = PositionManager(trade_action_queue, shutdown_event)
    
    # --- Start listener FIRST to capture all fills ---
    # It needs to be running before we open the initial straddle.
    fill_listener_task = asyncio.create_task(position_manager.fill_listener_loop())
    await asyncio.sleep(5) # Wait for the listener to subscribe to the trade updates

    # --- Perform one-time setup ---
    await position_manager.initialize_position()

    # If in 'init' mode, also open the initial options position
    # Client for fetching stock data, needed for strangle opening
    if config.INITIALIZATION_MODE == 'init':
        await open_initial_straddle(position_manager)
    
    if position_manager.call_option_symbol is None or position_manager.put_option_symbol is None:
        logger.critical("Failed to find an initial straddle position. Cannot start strategy. Exiting.")
        fill_listener_task.cancel() # Don't leave the task running
        return # Exit the main function gracefully
    
    # --- Initialize Communication Queues ---
    # Used to trigger the Delta Engine to start a calculation
    trigger_queue = asyncio.Queue(maxsize=1)
    # Used to send calculated options delta from the Engine to the Strategy
    delta_queue = asyncio.Queue(maxsize=1)

    
    # --- Instantiate Components and Inject Dependencies ---
    logger.info("Initializing application components...")
    
    market_manager = MarketDataManager(
        trigger_queue, 
        position_manager.call_option_symbol, 
        position_manager.put_option_symbol
    )
    
    delta_engine = DeltaEngine(market_manager, trigger_queue, delta_queue, shutdown_event)
    
    trading_strategy = TradingStrategy(position_manager, delta_queue, trade_action_queue, shutdown_event)

    # --- Define all long-running tasks ---
    tasks = [
        market_manager.run(),
        position_manager.trade_executor_loop(),
        delta_engine.run(),
        trading_strategy.run()
    ]
    
    # Add the listener task to the list of tasks to manage
    tasks.append(fill_listener_task)

    logger.info("Application starting. Press Ctrl+C to shut down gracefully.")
    
    # --- Run until interrupted ---
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Cleaning up...")
        shutdown_event.set()
        # Allow tasks to finish cleanly
        await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info("Application has shut down.")

if __name__ == "__main__":
    asyncio.run(main())
