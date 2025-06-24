import re
from datetime import datetime

def parse_option_symbol(symbol):
    """
    Parses OCC-style option symbol to extract its components.

    Example:
        'AAPL250516P00207500' -> ('AAPL', 'P', datetime.date(2025, 5, 16), 207.5)
    """
    # Pad symbol with leading spaces to 21 characters if it's shorter
    # This handles cases where the underlying symbol is less than 6 chars.
    symbol = symbol.rjust(21)
    
    match = re.match(r'^(?P<underlying>[A-Z ]+)(?P<date>\d{6})(?P<type>[PC])(?P<strike>\d{8})$', symbol)
    
    if match:
        parts = match.groupdict()
        underlying = parts['underlying'].strip() # Remove padding
        option_type = parts['type']
        
        # Parse date and strike
        expiration_date = datetime.strptime(parts['date'], '%y%m%d').date()
        strike_price = int(parts['strike']) / 1000.0
        
        return underlying, option_type, expiration_date, strike_price
    else:
        raise ValueError(f"Invalid option symbol format: {symbol}") 