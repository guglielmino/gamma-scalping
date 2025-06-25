import datetime
import pandas as pd
import numpy as np
from scipy.interpolate import interp1d
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)

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
    """Fetches CSV data from the given URL and parses it."""
    logger.info(f"Fetching treasury data from treasury.gov")
    return pd.read_csv(url)


def get_yield_curve():
    today = datetime.date.today()
    year, month = today.year, today.month
    try:
        url = build_treasury_url(year, month)
        df = fetch_and_parse_treasury_data(url)
    except:
        if month == 1:
            year -= 1
            month = 12
        else:
            month -= 1
        url = build_treasury_url(year, month)
        df = fetch_and_parse_treasury_data(url)
    df['Date'] = pd.to_datetime(df.Date)
    df.set_index('Date', inplace=True)
    latest_curve = df.sort_index().iloc[-1]

    return latest_curve

def get_risk_free_rate(days_to_maturity):
    latest_curve = get_yield_curve()
    maturities = np.array([maturity_days[col] for col in latest_curve.index])
    yields = latest_curve.values.astype(float)
    interp = interp1d(maturities, yields, kind="linear", fill_value="extrapolate")
    risk_free_rate = round(interp(days_to_maturity)/100, 4)
    return float(risk_free_rate)
