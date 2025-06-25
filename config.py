# delta_hedger/config.py

import os
from dotenv import load_dotenv

# --- Load Environment Variables ---
load_dotenv(override=True)

IS_PAPER_TRADING = os.getenv("IS_PAPER_TRADING", "true").lower() == "true"
API_KEY = os.getenv("TRADING_API_KEY")
API_SECRET = os.getenv("TRADING_API_SECRET")

# --- Strategy & Ticker Configuration ---
# The underlying stock we are trading and hedging
HEDGING_ASSET = "PLTR"
# The directory to log trades to.
TRADE_LOG_DIR = "trades"
# --- Initialization Mode ---
# 'init' to start fresh (clears all positions), 'resume' to continue with existing positions.
INITIALIZATION_MODE = "init"


# --- Hedging Strategy Parameters ---
# The 'dead band' for our net delta. A trade is triggered if abs(net_delta) > this value.
# This prevents excessive trading due to small delta fluctuations and reduces transaction costs.
HEDGING_DELTA_THRESHOLD = 2 # e.g., +/- 5 shares
# Number of straddles (call/put pairs) to trade for the strategy.
STRATEGY_MULTIPLIER = 1
# Minimum days until expiration for options we consider for hedging.
MIN_EXPIRATION_DAYS = 30
# Maximum days until expiration for options we consider for hedging.
MAX_EXPIRATION_DAYS = 60
# Minimum open interest required for an option contract to be considered liquid enough for trading.
MIN_OPEN_INTEREST = 100
# Weight for the theta in the score calculation.
# A rough proxy for the number of days the position will be held.
THETA_WEIGHT = 5
# Default risk free rate.
# This is used if the yield curve cannot be fetched from treasury.gov
DEFAULT_RISK_FREE_RATE = 0.045


# --- Market State Trigger Parameters ---
# Trigger a delta calculation if the stock price moves by this amount.
PRICE_CHANGE_THRESHOLD = 0.05 # $0.05
# Or, trigger a delta calculation every X seconds regardless of price changes (heartbeat).
HEARTBEAT_TRIGGER_SECONDS = 5.0

# --- Position Manager Parameters ---
# Time-To-Live for trade commands. Stale commands will be discarded.
TRADE_COMMAND_TTL_SECONDS = 5.0

# Number of shares represented by a single options contract.
OPTIONS_CONTRACT_MULTIPLIER = 100

# --- Logging Configuration ---
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
LOG_LEVEL = "INFO"
