import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.data.requests import OptionSnapshotRequest
from alpaca.trading.enums import AssetStatus, ContractType, AssetClass, OrderSide, TimeInForce
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data import StockHistoricalDataClient
from config import (
    API_KEY, API_SECRET, HEDGING_ASSET,
    MIN_EXPIRATION_DAYS, MAX_EXPIRATION_DAYS, MIN_OPEN_INTEREST,
    STRATEGY_MULTIPLIER
)
from portfolio.position_manager import PositionManager
from datetime import datetime, timedelta
from engine.delta_engine import calculate_single_option_greeks

logger = logging.getLogger(__name__)

# MUST MAKE SURE OPTIONS NAMES ARE SAVED IN THE POSITION MANAGER
async def open_initial_straddle(position_manager: PositionManager):
    """
    Analyzes available option contracts to find and open the most favorable
    long straddle position. A long straddle involves buying a call and a put option
    with the same expiration date and strike price.

    The selection process is as follows:
    1.  Fetch all active call and put contracts within a specified expiration window.
    2.  Filter out contracts with low open interest to ensure liquidity.
    3.  Group contracts by expiration and identify "straddle pairs" (a call and put
        at the same strike price).
    4.  From these pairs, find the strike price closest to the current underlying price
        for each expiration date, ensuring it's within a 5% range.
    5.  Calculate a score for each potential straddle based on its theta (time decay)
        and gamma (rate of change of delta). The score is `abs(theta) / gamma`.
        A lower score is better, representing a position that has lower time decay
        relative to its gamma exposure. This is ideal for a gamma scalping strategy.
    6.  Select the straddle with the best (lowest) score.
    7.  Submit market orders to buy one of each of the selected call and put options.
    """
    logger.info("--- Beginning search for an initial straddle position ---")
    
    # Initialize API clients for trading and market data.
    trading_client = position_manager.trading_client
    stock_client = StockHistoricalDataClient(API_KEY, API_SECRET)
    option_client = OptionHistoricalDataClient(API_KEY, API_SECRET)

    # Step 1: Get the current price of the underlying asset.
    # We use the midpoint of the bid-ask spread as a proxy for the current market price.
    try:
        request_params = StockLatestQuoteRequest(symbol_or_symbols=HEDGING_ASSET)
        latest_quote = stock_client.get_stock_latest_quote(request_params)
        underlying_price = latest_quote[HEDGING_ASSET].ask_price / 2 + latest_quote[HEDGING_ASSET].bid_price / 2
        logger.info(f"Current price of {HEDGING_ASSET} is ${underlying_price:.2f}")
    except Exception as e:
        logger.error(f"Failed to get current price for {HEDGING_ASSET}: {e}")
        return
    
    # Step 2: Fetch all active option contracts that meet our date and liquidity criteria.
    logger.info(f"Fetching options for {HEDGING_ASSET} expiring between {MIN_EXPIRATION_DAYS} and {MAX_EXPIRATION_DAYS} days.")
    request_params = GetOptionContractsRequest(
        underlying_symbols=[HEDGING_ASSET],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=(datetime.now() + timedelta(days=MIN_EXPIRATION_DAYS)).date(),
        expiration_date_lte=(datetime.now() + timedelta(days=MAX_EXPIRATION_DAYS)).date(),
        root_symbol=HEDGING_ASSET,
        type=ContractType.CALL
    )
    call_contracts = trading_client.get_option_contracts(request_params).option_contracts
    request_params.type = ContractType.PUT
    put_contracts = trading_client.get_option_contracts(request_params).option_contracts

    # Filter out contracts with low open interest to avoid issues with liquidity.
    call_contracts = [contract for contract in call_contracts if contract.open_interest is not None and int(contract.open_interest) > MIN_OPEN_INTEREST]
    put_contracts = [contract for contract in put_contracts if contract.open_interest is not None and int(contract.open_interest) > MIN_OPEN_INTEREST]
    logger.info(f"Found {len(call_contracts)} eligible calls and {len(put_contracts)} eligible puts.")

    # Step 3: Group contracts by expiration date and strike price to find valid straddle pairs.
    # A valid pair consists of one call and one put for the same expiry and strike.
    contracts_by_expiry = {}
    for contract in call_contracts + put_contracts:
        expiry = contract.expiration_date
        strike = contract.strike_price
        if expiry not in contracts_by_expiry:
            contracts_by_expiry[expiry] = {}
        if strike not in contracts_by_expiry[expiry]:
            contracts_by_expiry[expiry][strike] = {}

        if contract.type == ContractType.CALL:
            contracts_by_expiry[expiry][strike]['call'] = contract
        else:
            contracts_by_expiry[expiry][strike]['put'] = contract

    # Step 4: Identify the best candidate straddles from the available pairs.
    # For each expiration, we find the strike price that is closest to the underlying price.
    best_straddles = []
    for expiry, strikes in contracts_by_expiry.items():
        best_strike = None
        min_diff = float('inf')

        for strike, contract_pair in strikes.items():
            if 'call' in contract_pair and 'put' in contract_pair:
                diff = abs(strike - underlying_price)
                if diff < min_diff:
                    min_diff = diff
                    best_strike = strike
        
        # We only consider straddles where the strike is within 5% of the underlying price.
        # This helps to ensure we are not trading extremely OTM options.
        if best_strike is not None and min_diff < 0.05 * underlying_price:
            best_straddles.append({
                'expiration': expiry,
                'strike': best_strike,
                'call': contracts_by_expiry[expiry][best_strike]['call'],
                'put': contracts_by_expiry[expiry][best_strike]['put']
            })
    
    if not best_straddles:
        logger.warning("Could not find any suitable straddle pairs within the given criteria.")
        return
    
    logger.info(f"Identified {len(best_straddles)} potential straddle candidates. Now scoring them...")
    # Step 5: Score each candidate straddle to find the most favorable one.
    # The score is calculated as abs(Total Theta) / Total Gamma. A lower score is better.
    # This ratio helps us find a straddle that has low time decay (theta) for the amount of convexity (gamma) it provides.
    for straddle in best_straddles:
        try:
            call_symbol = straddle['call'].symbol
            put_symbol = straddle['put'].symbol
            
            # Get the latest mid-price for both the call and the put option using a real-time snapshot.
            snapshot_request = OptionSnapshotRequest(symbol_or_symbols=[call_symbol, put_symbol])
            snapshots = option_client.get_option_snapshot(snapshot_request)

            # print(snapshots)
            
            call_price = (snapshots[call_symbol].latest_quote.ask_price + snapshots[call_symbol].latest_quote.bid_price) / 2
            put_price = (snapshots[put_symbol].latest_quote.ask_price + snapshots[put_symbol].latest_quote.bid_price) / 2
            
            expiry_days = (straddle['expiration'] - datetime.now().date()).days
            
            # Calculate the greeks (theta and gamma) for both options.
            # We use a placeholder risk-free rate of 5% and no dividend yield.
            call_greeks = calculate_single_option_greeks(
                call_price, underlying_price, straddle['strike'], expiry_days, 'call', 0.05, 0, ['theta', 'gamma']
            )
            put_greeks = calculate_single_option_greeks(
                put_price, underlying_price, straddle['strike'], expiry_days, 'put', 0.05, 0, ['theta', 'gamma']
            )
            
            call_theta = call_greeks['theta']
            call_gamma = call_greeks['gamma']
            put_theta = put_greeks['theta']
            put_gamma = put_greeks['gamma']
            
            # If the greeks calculation fails (returns NaN), we disqualify the straddle.
            if any(v != v for v in [call_theta, call_gamma, put_theta, put_gamma]):
                logger.warning(f"Greeks calculation resulted in NaN for {straddle['call'].symbol} or {straddle['put'].symbol}. Skipping.")
                straddle['score'] = float('inf')
                continue

            # Calculate the final score for the straddle.
            total_theta = call_theta + put_theta
            total_gamma = call_gamma + put_gamma
            
            if total_gamma > 0:
                straddle['score'] = abs(total_theta) / total_gamma
            else:
                # Avoid division by zero if gamma is not positive.
                straddle['score'] = float('inf')

        except Exception as e:
            logger.error(f"Failed to score straddle for expiry {straddle['expiration']}: {e}")
            straddle['score'] = float('inf')

    # Step 6: Select the straddle with the lowest score.
    best_straddles.sort(key=lambda x: x.get('score', float('inf')))

    if not best_straddles or best_straddles[0].get('score') == float('inf'):
        logger.warning("No suitable straddles could be scored successfully.")
        return
    
    # The best straddle is the one with the lowest score at the top of the sorted list.
    chosen_straddle = best_straddles[0]
    call_symbol = chosen_straddle['call'].symbol
    put_symbol = chosen_straddle['put'].symbol
    
    logger.info(f"Best straddle found: Expiry {chosen_straddle['expiration']}, Strike {chosen_straddle['strike']}, Score: {chosen_straddle['score']:.4f}")
    logger.info(f"Call: {call_symbol}, Put: {put_symbol}")

    # Step 7: Submit market orders to open the long straddle position.
    try:
        # Buy 1 Call
        call_order_request = MarketOrderRequest(
            symbol=call_symbol,
            qty=STRATEGY_MULTIPLIER,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=call_order_request)
        logger.info(f"Submitted market order to BUY {STRATEGY_MULTIPLIER} {call_symbol}")

        # Buy 1 Put
        put_order_request = MarketOrderRequest(
            symbol=put_symbol,
            qty=STRATEGY_MULTIPLIER,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order_data=put_order_request)
        logger.info(f"Submitted market order to BUY {STRATEGY_MULTIPLIER} {put_symbol}")

        # Update the position manager with the new option symbols
        position_manager.call_option_symbol = call_symbol
        position_manager.put_option_symbol = put_symbol

    except Exception as e:
        logger.error(f"Failed to submit initial straddle orders: {e}") 