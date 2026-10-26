-- Drop existing tables in reverse dependency order
DROP TABLE IF EXISTS daily_portfolio_snapshots CASCADE;
DROP TABLE IF EXISTS market_prices CASCADE;
DROP TABLE IF EXISTS asset_prices CASCADE;
DROP TABLE IF EXISTS historical_asset_info CASCADE;
DROP TABLE IF EXISTS asset_splits CASCADE;
DROP TABLE IF EXISTS fx_rates CASCADE;
DROP TABLE IF EXISTS transactions CASCADE;
DROP TABLE IF EXISTS assets CASCADE;
DROP TABLE IF EXISTS accounts CASCADE;

-- Table Definitions
-- 1. ACCOUNTS (Brokers, Wallets, Banks, Physical Holdings)
CREATE TABLE accounts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL UNIQUE,       -- e.g., 'Schwab', 'BitBox Cold Storage', 'Tesouro / NuInvest', 'Wise EUR'
    account_type VARCHAR(32) NOT NULL       -- 'brokerage', 'crypto_wallet', 'bank', 'fixed_income', 'physical'
);

-- 2. ASSETS
CREATE TABLE assets (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) NOT NULL UNIQUE,     -- 'VT', 'BTC', 'TD_IPCA_2035', 'GROUND_SEBASTIAO', 'EUR', 'BRL'
    asset_class VARCHAR(32) NOT NULL,       -- 'equity', 'crypto', 'fixed_income', 'real_estate', 'cash'
    currency VARCHAR(3) NOT NULL,           -- Native pricing currency ('USD', 'BRL', 'EUR')
    is_liquid BOOLEAN DEFAULT TRUE          -- FALSE for ground/real estate
);

-- 3. TRANSACTIONS (pure quantity ledger - every dollar amount is derived at read time from
-- quantity x asset_prices.price, never stored redundantly on the transaction itself)
CREATE TABLE transactions (
    id SERIAL PRIMARY KEY,
    account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    asset_id INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tx_type VARCHAR(32) NOT NULL,           -- 'BUY', 'SELL', 'DEPOSIT', 'WITHDRAWAL', 'DIVIDEND', 'INTEREST', 'FEE'
    executed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    quantity NUMERIC(18, 8) NOT NULL,       -- Positive for buys/inflows, negative for sells/outflows (supports BTC satoshis)
    notes TEXT
);

-- Speeds up snapshot.py's per-day "transactions as of this date" scan (matches the exact
-- t.executed_at::date cast used there, so it's usable as an expression index).
CREATE INDEX idx_transactions_asset_executed_date ON transactions (asset_id, (executed_at::date));

-- Same as above, but account_id-led - covers the read-side account-only scope (e.g. viewing a
-- whole account's cash flows/asset list in get_external_cash_flows/analytics.py), which the
-- asset-led index above can't serve since no asset_id filter is present in that case.
CREATE INDEX idx_transactions_account_executed_date ON transactions (account_id, (executed_at::date));

-- 4a. ASSET PRICES (also doubles as FX rates: a currency is just a 'cash' asset whose price
-- history is its own HUB_CURRENCY value - see analytics/utils.py). `price` is always the
-- nominal, as-actually-traded value for that date - NOT split-adjusted (see asset_splits below
-- and ingest/market_data/yahoo.py, since yfinance's own history() always returns split-adjusted
-- closes regardless of auto_adjust).
CREATE TABLE asset_prices (
    id SERIAL PRIMARY KEY,
    asset_id INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    price_date DATE NOT NULL,
    price NUMERIC(18, 4) NOT NULL,
    CONSTRAINT unique_asset_price_date UNIQUE (asset_id, price_date)
);

-- 4b. ASSET SPLITS (corporate actions - equities only, effective-date events). Consumers
-- reconstruct whatever basis they need at read time: ingest/snapshot.py forward-adjusts a
-- transaction's raw quantity by the cumulative ratio of every split after it (bounded by the
-- snapshot's target_date), while analytics/utils.py's get_daily_price_series reverses the same
-- factor onto price for continuous, non-jumpy charts.
CREATE TABLE asset_splits (
    asset_id INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    split_date DATE NOT NULL,
    ratio NUMERIC(18, 8) NOT NULL, -- new_shares/old_shares on the split's effective date
    PRIMARY KEY (asset_id, split_date)
);

-- 5. DAILY VALUATION SNAPSHOTS (Materialized / Derived Cache)
CREATE TABLE daily_portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    asset_id INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    quantity_held NUMERIC(18, 8) NOT NULL,
    market_value_base NUMERIC(18, 4) NOT NULL,
    CONSTRAINT unique_daily_snapshot UNIQUE (snapshot_date, account_id, asset_id)
);
