import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data import StockHistoricalDataClient
from alpaca.trading.enums import AssetStatus, ContractType, AssetClass
from alpaca.trading.client import TradingClient


from config import (
    API_KEY, API_SECRET, IS_PAPER_TRADING, HEDGING_ASSET,
    MIN_EXPIRATION_DAYS, MAX_EXPIRATION_DAYS, MIN_OPEN_INTEREST
)
from portfolio.position_manager import PositionManager
from datetime import datetime, timedelta


"""
Opens an initial long strangle position by buying an OTM call and an OTM put.
"""
print("--- Opening Initial Strangle Position ---")
stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
trading_client = TradingClient(API_KEY, API_SECRET)


# 1. Get the current price of the underlying asset

request_params = StockLatestQuoteRequest(symbol_or_symbols=HEDGING_ASSET)
latest_quote = stock_client.get_stock_latest_quote(request_params)
current_price = latest_quote[HEDGING_ASSET].ask_price / 2 + latest_quote[HEDGING_ASSET].bid_price / 2
print(f"Current price of {HEDGING_ASSET} is ${current_price:.2f}")

# 2. Get options contracts that meet our criteria
request_params = GetOptionContractsRequest(
    underlying_symbols=[HEDGING_ASSET],
    status=AssetStatus.ACTIVE,
    expiration_date_gte=(datetime.now() + timedelta(days=MIN_EXPIRATION_DAYS)).date(),
    expiration_date_lte=(datetime.now() + timedelta(days=MAX_EXPIRATION_DAYS)).date(),
    root_symbol=HEDGING_ASSET,
    type=ContractType.CALL
)
contracts = trading_client.get_option_contracts(request_params)
print(contracts)