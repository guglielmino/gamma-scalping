# Gamma Scalper: An Automated Market-Neutral Options Strategy

This project provides a reference implementation of an automated Gamma Scalping strategy using the Alpaca Trading API. It is designed to be a robust, high-performance starting point for traders and developers interested in sophisticated, market-neutral options strategies.

**Disclaimer:** This project is for educational and informational purposes only. Trading financial instruments, especially options, involves substantial risk and is not suitable for all investors. The developers and Alpaca assume no responsibility for any financial losses incurred as a result of using this software. **Always run in a paper trading environment first.**

---

## Strategy Overview

Gamma Scalping is a market-neutral strategy that seeks to profit from the volatility of an underlying asset. The core idea is to hold a **long gamma** position and then continuously hedge its **delta** to remain neutral.

1.  **Establish a Long Straddle:** The strategy begins by buying a call and a put option on the same underlying asset (`SPY` by default) with the same strike price and expiration date. This creates a position that is:
    *   **Positive Gamma:** The position's delta accelerates in the direction of a price move (i.e., it becomes more positive as the price goes up and more negative as it goes down).
    *   **Initially Delta-Neutral:** The combined delta of the call and put is close to zero, meaning the position has no initial directional bias.

2.  **Delta Hedging (The "Scalp"):** As the price of the underlying asset fluctuates, the position's delta will shift. The bot continuously monitors this delta and executes trades in the underlying stock to counteract the change.
    *   If the stock price rises, the straddle's delta becomes positive. The bot sells shares to bring the total portfolio delta back to zero.
    *   If the stock price falls, the straddle's delta becomes negative. The bot buys shares to bring the delta back to zero.

3.  **Profit Source:** The strategy aims to generate more profit from these small, frequent "scalping" trades than it loses from the options' time decay (theta). In essence, it is a bet that the *realized volatility* of the asset will be greater than the *implied volatility* priced into the options.

## System Architecture

The application is built using Python's `asyncio` library for high-performance, concurrent operations. Components are decoupled and communicate via queues, creating a resilient and scalable architecture.

```mermaid
flowchart TD
    subgraph "External Systems"
        A[Alpaca Market Data API]
        B[Alpaca Trading API]
    end

    subgraph "Gamma Scalper Application"
        C[MarketDataStreamer]
        D[MarketState]
        E[DeltaEngine]
        F[TradingStrategy]
        G[PositionManager]
        H(trigger_queue)
        I(delta_queue)
        J(trade_action_queue)
    end

    A -- "Stock & Option Quotes" --> C
    C -- "Update Prices" --> D
    D -- "Price moves > threshold<br/>OR heartbeat" --> H
    H -- "CALCULATE_DELTA" --> E
    D -- "Get Market Data" --> E
    E -- "Calculated Greeks (Δ, Θ, Γ)" --> I
    I -- "Greeks" --> F
    G -- "Get Position State" --> F
    F -- "Net Δ > threshold" --> J
    J -- "Trade Command" --> G
    G -- "Submit/Cancel/Close Orders" --> B
    B -- "Trade Fill Updates" --> G
```

### Key Components

*   **`options_strategy.py`:** Intelligently screens for and selects the optimal straddle to purchase based on liquidity, time to expiration, and a `abs(theta)/gamma` score to find the most "gamma-cheap" options.
*   **`market/state.py`:** Subscribes to real-time Alpaca data streams for the stock and options, holding the latest prices and triggering calculations.
*   **`engine/delta_engine.py`:** Utilizes the industry-standard `QuantLib` library to perform high-precision calculations for option Greeks. It specifically uses a binomial tree pricing model (Cox-Ross-Rubinstein), which is necessary for accurately valuing American-style options by accounting for the possibility of early exercise.
*   **`strategy/hedging_strategy.py`:** The "brain" of the operation. It consumes Greeks, calculates the portfolio's net delta, and decides when to send a hedging trade command.
*   **`portfolio/position_manager.py`:** Manages the portfolio state, executes trades via the Alpaca API, and listens for fill confirmations to robustly track the current position.

## Getting Started

### 1. Prerequisites
* Python 3.10+
* An Alpaca paper or live trading account.
* [uv](https://github.com/astral-sh/uv) (for environment and package management)

### 2. Installation

This project uses `uv` for fast environment and package management.

1.  **Install `uv`**

    If you don't have `uv` installed, you can often install it with `pip`:
    ```bash
    pip install uv
    ```

2.  **Clone the repository and set up the environment**

    ```bash
    # Clone the repository
    git clone https://github.com/alpacahq/gamma-scalper.git
    cd gamma-scalper

    # Create and activate a virtual environment with uv
    uv venv
    source .venv/bin/activate # On Windows, use `.venv\Scripts\activate`

    # Install the project dependencies in editable mode
    uv pip install -e .
    ```

### 3. Configuration

Create a `.env` file in the project root by copying the example:

```bash
cp .env.example .env
```

Now, edit the `.env` file with your Alpaca API keys and desired settings:

```
# .env file
IS_PAPER_TRADING="true"
TRADING_API_KEY="YOUR_PAPER_API_KEY"
TRADING_API_SECRET="YOUR_PAPER_API_SECRET"
```

You can also adjust the strategy parameters in `config.py`. Key parameters include:
*   `HEDGING_ASSET`: The underlying asset to trade (e.g., "SPY").
*   `HEDGING_DELTA_THRESHOLD`: The "dead band" for delta. A hedge is triggered if `abs(net_delta)` exceeds this.
*   `MIN/MAX_EXPIRATION_DAYS`: The window for selecting option contracts.
*   `PRICE_CHANGE_THRESHOLD` / `HEARTBEAT_TRIGGER_SECONDS`: Conditions for triggering a new delta calculation.

### 4. Running the Bot

```bash
python main.py
```

The bot will start, initialize its position (or find a new one if in `init` mode), and begin hedging.

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details. 


NEXT STEPS
    - find the opportunities (need to predict vol and find mispricings)
    - dynamic delta threshold
    - exits (when has the oppportunity passed, stop loss, letting it run)
    - improved pricing models (discrete dividends, finer grained yield curves)