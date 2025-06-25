import datetime
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
import logging
from config import DEFAULT_RISK_FREE_RATE

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

# Corrected maturity mapping to match treasury.gov CSV columns
maturity_days = {
    "1 Mo": 30,
    "1.5 Month": 45,
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
    """Constructs the URL for the given year and month to download the daily treasury yield curve."""
    month_str = f"{month:02d}"
    year_month = f"{year}{month_str}"
    
    # This URL structure is based on the one you provided.
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv"
        f"/all/{year_month}?type=daily_treasury_yield_curve&field_tdr_date_value_month={year_month}&page&_format=csv"
    )
    return url

def fetch_and_parse_treasury_data(url):
    """Fetches CSV data from the given URL and parses it into a pandas DataFrame."""
    logger.info(f"Attempting to fetch treasury data from: {url}")
    try:
        df = pd.read_csv(url)
        logger.info("Successfully fetched and parsed treasury data.")
        return df
    except Exception as e:
        logger.error(f"Failed to fetch or parse treasury data. Error: {e}")
        return None


def get_yield_curve():
    """
    Fetches the most recent daily yield curve data from treasury.gov.
    Tries the current month first, then the previous month as a fallback.
    """
    today = datetime.date.today()
    year, month = today.year, today.month

    # Try fetching data for the current month
    logger.info(f"Attempting to fetch yield curve for current month: {year}-{month:02d}")
    url = build_treasury_url(year, month)
    df = fetch_and_parse_treasury_data(url)

    # If current month fails (e.g., data not yet published), try the previous month
    if df is None or df.empty:
        logger.warning(f"Failed to get data for {year}-{month:02d}. Trying previous month as fallback.")
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        url = build_treasury_url(year, month)
        df = fetch_and_parse_treasury_data(url)

    # If still no data after fallback, it's a critical failure
    if df is None or df.empty:
        logger.error("Could not retrieve treasury yield curve data after fallback. Aborting.")
        return None

    try:
        df['Date'] = pd.to_datetime(df.Date)
        df.set_index('Date', inplace=True)
        latest_curve = df.sort_index().iloc[-1]
        logger.info(f"Successfully processed yield curve for date: {latest_curve.name.date()}")
        return latest_curve
    except Exception as e:
        logger.error(f"Failed to process the fetched dataframe into a yield curve. Error: {e}")
        return None


def get_risk_free_rate(days_to_maturity):
    """
    Calculates the risk-free rate for a given maturity by interpolating the latest yield curve.
    """
    logger.info(f"Calculating risk-free rate for {days_to_maturity} days to maturity.")
    latest_curve = get_yield_curve()
    
    # Fallback if the yield curve could not be fetched
    if latest_curve is None:
        default_rate = DEFAULT_RISK_FREE_RATE
        logger.warning(f"Could not get yield curve. Falling back to default risk-free rate: {default_rate:.2%}")
        return default_rate
        
    try:
        # Filter out any non-numeric columns and ensure they are in our maturity map
        valid_columns = [col for col in latest_curve.index if col in maturity_days and pd.notna(latest_curve[col])]
        
        if len(valid_columns) < 2:
            default_rate = DEFAULT_RISK_FREE_RATE
            logger.error("Not enough data points on the yield curve to perform interpolation.")
            logger.warning(f"Falling back to default risk-free rate: {default_rate:.2%}")
            return default_rate

        maturities = np.array([maturity_days[col] for col in valid_columns])
        yields = latest_curve[valid_columns].values.astype(float)
        
        # Sort by maturity to ensure correct interpolation
        sort_indices = maturities.argsort()
        maturities = maturities[sort_indices]
        yields = yields[sort_indices]
        
        interp = interp1d(maturities, yields, kind="linear", fill_value="extrapolate")
        risk_free_rate = round(interp(days_to_maturity)/100, 4)
        logger.info(f"Interpolated risk-free rate is: {risk_free_rate:.4%}")
        
        return float(risk_free_rate)
    except Exception as e:
        default_rate = DEFAULT_RISK_FREE_RATE
        logger.error(f"An error occurred during interpolation: {e}")
        logger.warning(f"Falling back to default risk-free rate: {default_rate:.2%}")
        return default_rate
