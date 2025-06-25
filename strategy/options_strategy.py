import logging
import math
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
    STRATEGY_MULTIPLIER, THETA_WEIGHT
)
from portfolio.position_manager import PositionManager
from datetime import datetime, timedelta
from engine.delta_engine import calculate_single_option_greeks, calculate_implied_volatility

logger = logging.getLogger(__name__)

async def _fetch_all_contracts(trading_client: TradingClient, request_params: GetOptionContractsRequest):
    """
    Fetches all available option contracts by handling API pagination.
    """
    all_contracts = []
    page_token = None
    
    while True:
        request_params.page_token = page_token
        response = trading_client.get_option_contracts(request_params)
        
        if response.option_contracts:
            all_contracts.extend(response.option_contracts)
        
        page_token = response.next_page_token
        if not page_token:
            break
            
    return all_contracts

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
    call_contracts = await _fetch_all_contracts(trading_client, request_params)
    request_params.type = ContractType.PUT
    put_contracts = await _fetch_all_contracts(trading_client, request_params)

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

    # Step 4: For each expiration, determine a dynamic strike range based on implied volatility.
    all_candidate_straddles = []
    for expiry, strikes in contracts_by_expiry.items():
        # Find the at-the-money strike to use as a baseline for IV
        atm_strike = min(strikes.keys(), key=lambda k: abs(k - underlying_price))
        
        # Ensure the ATM strike has a valid straddle pair before proceeding
        if not ('call' in strikes[atm_strike] and 'put' in strikes[atm_strike]):
            logger.warning(f"No valid ATM straddle found for {expiry}. Skipping this expiration.")
            continue
            
        try:
            # --- Calculate Dynamic Strike Range ---
            atm_call_symbol = strikes[atm_strike]['call'].symbol
            snapshot_request = OptionSnapshotRequest(symbol_or_symbols=[atm_call_symbol])
            snapshot = option_client.get_option_snapshot(snapshot_request)[atm_call_symbol]
            atm_call_price = (snapshot.latest_quote.ask_price + snapshot.latest_quote.bid_price) / 2
            
            expiry_days = (expiry - datetime.now().date()).days
            time_to_expiry_years = expiry_days / 365.25

            # Use the existing function to get a baseline IV from the ATM call

            #TODO: update the risk free rate and dividend yield
            baseline_iv = calculate_implied_volatility(
                atm_call_price, underlying_price, atm_strike, expiry_days, 'call', 0.05, 0
            )
            
            if not (baseline_iv > 0): # Check for NaN or non-positive IV
                logger.warning(f"Could not calculate a valid baseline IV for ATM strike {atm_strike} on {expiry}. Skipping.")
                continue

            # Define the strike search range using a 1-standard-deviation expected move
            expected_move = underlying_price * baseline_iv * math.sqrt(time_to_expiry_years)
            min_strike = underlying_price - expected_move
            max_strike = underlying_price + expected_move
            logger.info(f"For expiry {expiry}, baseline IV is {baseline_iv:.2%}. Expected move: ${expected_move:.2f}. Strike Range: {min_strike:.2f}-{max_strike:.2f}")

            # Gather all valid straddles within this dynamic range
            for strike, contract_pair in strikes.items():
                if 'call' in contract_pair and 'put' in contract_pair and min_strike <= strike <= max_strike:
                    all_candidate_straddles.append({
                        'expiration': expiry,
                        'strike': strike,
                        'call': contract_pair['call'],
                        'put': contract_pair['put']
                    })

        except Exception as e:
            logger.error(f"Error processing expiration {expiry}: {e}")
            continue

    if not all_candidate_straddles:
        logger.warning("Could not find any suitable straddle pairs within the dynamic criteria.")
        return
    
    logger.info(f"Identified {len(all_candidate_straddles)} potential straddle candidates across all expiries. Now scoring them...")
    
    # Step 5: Score all candidate straddles to find the most favorable one.
    for straddle in all_candidate_straddles:
        try:
            call_symbol = straddle['call'].symbol
            put_symbol = straddle['put'].symbol
            
            # Get the latest mid-price for both the call and the put option using a real-time snapshot.
            snapshot_request = OptionSnapshotRequest(symbol_or_symbols=[call_symbol, put_symbol])
            snapshots = option_client.get_option_snapshot(snapshot_request)

            call_price = (snapshots[call_symbol].latest_quote.ask_price + snapshots[call_symbol].latest_quote.bid_price) / 2
            put_price = (snapshots[put_symbol].latest_quote.ask_price + snapshots[put_symbol].latest_quote.bid_price) / 2
            call_spread = snapshots[call_symbol].latest_quote.ask_price - snapshots[call_symbol].latest_quote.bid_price
            put_spread = snapshots[put_symbol].latest_quote.ask_price - snapshots[put_symbol].latest_quote.bid_price
            
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
            total_spread = call_spread + put_spread
            
            if total_gamma > 0:
                straddle['score'] = (abs(total_theta) * THETA_WEIGHT + total_spread ) / total_gamma
            else:
                # Avoid division by zero if gamma is not positive.
                straddle['score'] = float('inf')

        except Exception as e:
            logger.error(f"Failed to score straddle for expiry {straddle['expiration']}: {e}")
            straddle['score'] = float('inf')

    # Step 6: Select the straddle with the lowest score from all candidates.
    all_candidate_straddles.sort(key=lambda x: x.get('score', float('inf')))

    if not all_candidate_straddles or all_candidate_straddles[0].get('score') == float('inf'):
        logger.warning("No suitable straddles could be scored successfully.")
        return
    
    # The best straddle is the one with the lowest score at the top of the sorted list.
    chosen_straddle = all_candidate_straddles[0]
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