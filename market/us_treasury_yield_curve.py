"""
Provides functionality to fetch and process U.S. Treasury yield curve data.

This module is essential for obtaining a reliable, real-world risk-free interest
rate, a critical input for accurate options pricing models like the one used in
the DeltaEngine. It fetches the latest available daily yield curve data directly
from the U.S. Treasury's official website.

The primary function, `get_risk_free_rate`, takes a specific time to maturity
and interpolates the yield curve to find the precise rate for that duration.
This is superior to using a single, static risk-free rate, as it accounts for
the term structure of interest rates.
"""

import datetime
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
import logging
from config import DEFAULT_RISK_FREE_RATE

# Configure logging for this module
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# This dictionary maps the column headers from the Treasury's CSV file to the
# corresponding number of days in the maturity period. This is the basis for
# our interpolation.
maturity_days = {
    "1 Mo": 30,
    "1.5 Month": 45,  # Note: This matches the Treasury.gov column names
    "2 Mo": 60,
    "3 Mo": 90,
    "4 Mo": 120,
    "6 Mo": 180,
    "1 Yr": 365,
    "2 Yr": 730,
    "3 Yr": 1095,
    "5 Yr": 1825,
    "7 Yr": 2555,
    "10 Yr": 3650,
    "20 Yr": 7300,
    "30 Yr": 10950,
}


def build_treasury_url(year, month):
    """
    Constructs the specific URL to download the daily treasury yield curve CSV
    for a given year and month from the Treasury.gov website.
    """
    month_str = f"{month:02d}"
    year_month = f"{year}{month_str}"
    
    # The URL format is specific to the Treasury's data portal.
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
        f"/all/{year_month}?type=daily_treasury_yield_curve&field_tdr_date_value_month={year_month}&page&_format=csv"
    )
    return url

def fetch_and_parse_treasury_data(url):
    """
    Fetches CSV data from the given Treasury URL and parses it into a pandas DataFrame.
    Includes robust error handling in case the URL is unreachable or data is malformed.
    """
    logger.info(f"Attempting to fetch treasury data from: {url}")
    try:
        # Use pandas to directly read the CSV data from the URL.
        df = pd.read_csv(url)
        logger.info("Successfully fetched and parsed treasury data.")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch or parse treasury data. Error: {e}")
        return None


def get_yield_curve():
    """
    Fetches the most recent daily yield curve data from Treasury.gov.

    It first attempts to get data for the current month. If that fails (e.g.,
    at the beginning of a month before data is published), it automatically
    falls back to trying the previous month. This makes the function resilient
    to timing issues.
    
    Returns:
        A pandas Series representing the latest available yield curve, or None if
        data could not be retrieved.
    """
    today = datetime.date.today()
    year, month = today.year, today.month

    # First attempt: current month.
    logger.info(f"Attempting to fetch yield curve for current month: {year}-{month:02d}")
    url = build_treasury_url(year, month)
    df = fetch_and_parse_treasury_data(url)

    # Fallback: previous month.
    if df is None or df.empty:
        logger.warning(f"Failed to get data for {year}-{month:02d}. Trying previous month as fallback.")
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        url = build_treasury_url(year, month)
        df = fetch_and_parse_treasury_data(url)

    # If both attempts fail, we cannot proceed.
    if df is None or df.empty:
        logger.error("Could not retrieve treasury yield curve data after fallback.")
        return None

    try:
        # Process the DataFrame to get the single most recent row of data.
        df['Date'] = pd.to_datetime(df['Date'])
        df.set_index('Date', inplace=True)
        latest_curve = df.sort_index().iloc[-1]
        logger.info(f"Successfully processed yield curve for date: {latest_curve.name.date()}")
        return latest_curve
    except Exception as e:
        logger.error(f"Failed to process the fetched dataframe into a yield curve. Error: {e}")
        return None


def get_risk_free_rate(days_to_maturity: int) -> float:
    """
    Calculates the risk-free rate for a specific maturity by interpolating the latest yield curve.

    This function is the public interface of this module. It fetches the curve,
    prepares the data points (maturities and yields), and then uses linear
    interpolation to find the yield for the exact number of days to maturity
    of our option. If fetching or interpolation fails, it returns a default rate.

    Args:
        days_to_maturity: The number of days until the option expires.

    Returns:
        The interpolated risk-free rate as a float (e.g., 0.05 for 5%).
    """
    logger.info(f"Calculating risk-free rate for {days_to_maturity} days to maturity.")
    latest_curve = get_yield_curve()
    
    # If we couldn't get the curve, log a warning and use the configured default.
    if latest_curve is None:
        default_rate = DEFAULT_RISK_FREE_RATE
        logger.warning(f"Could not get yield curve. Falling back to default risk-free rate: {default_rate:.2%}")
        return default_rate
        
    try:
        # Prepare the data for interpolation.
        # 1. Filter out columns that aren't in our maturity map or have null values.
        valid_columns = [col for col in latest_curve.index if col in maturity_days and pd.notna(latest_curve[col])]
        
        # 2. Check if we have enough data points to create an interpolation function.
        if len(valid_columns) < 2:
            default_rate = DEFAULT_RISK_FREE_RATE
            logger.error("Not enough data points on the yield curve to perform interpolation.")
            logger.warning(f"Falling back to default risk-free rate: {default_rate:.2%}")
            return default_rate

        # 3. Create numpy arrays of maturities (in days) and corresponding yields.
        maturities = np.array([maturity_days[col] for col in valid_columns])
        yields = latest_curve[valid_columns].values.astype(float)
        
        # 4. Sort both arrays by maturity to ensure the x-axis is monotonic for interpolation.
        sort_indices = maturities.argsort()
        maturities = maturities[sort_indices]
        yields = yields[sort_indices]
        
        # 5. Create the interpolation function using scipy.
        # 'kind="linear"' means we draw straight lines between the data points.
        # 'fill_value="extrapolate"' means if days_to_maturity is outside the range
        # of our data, we extend the line to estimate a value.
        interp_func = interp1d(maturities, yields, kind="linear", fill_value="extrapolate")
        
        # 6. Call the function with our target maturity and scale the result (it's in %).
        risk_free_rate = round(interp_func(days_to_maturity) / 100, 4)
        logger.info(f"Interpolated risk-free rate is: {risk_free_rate:.4%}")
        
        return float(risk_free_rate)
    except Exception as e:
        default_rate = DEFAULT_RISK_FREE_RATE
        logger.error(f"An error occurred during interpolation: {e}")
        logger.warning(f"Falling back to default risk-free rate: {default_rate:.2%}")
        return default_rate
