import datetime
import math
import yfinance as yf
from config import cfg
from analytics.utils import HUB_CURRENCY
from ingest.db_utils import upsert_asset_prices, upsert_asset_splits, get_or_create_asset


def _valid(value) -> bool:
    """True for a real, usable price/rate - filters out None, 0, and NaN (yfinance/pandas can
    return NaN for a data hiccup, which is truthy in Python and would otherwise slip through)."""
    return bool(value) and not math.isnan(value)


class YahooMarketData:
    """FX rates, equity/ETF, crypto, and benchmark prices via yfinance. One history call per
    symbol covers inception-to-today; always fetched with auto_adjust=False and un-adjusted
    back to nominal (as-traded) price, since dividends are already tracked separately."""

    def __init__(self, db_conn):
        self.conn = db_conn

    def _tracked_currencies(self, cur) -> list:
        """Every currency that actually needs a rate to the hub: whatever's in use by assets,
        plus anything configured, minus the hub itself."""
        cur.execute("SELECT DISTINCT currency FROM assets;")
        currencies = {r[0] for r in cur.fetchall()}
        currencies.update(cfg.TRACKED_CURRENCIES)
        currencies.discard(HUB_CURRENCY)
        return sorted(currencies)

    @staticmethod
    def _inception_date(cur) -> datetime.date:
        cur.execute("SELECT MIN(executed_at)::date FROM transactions;")
        row = cur.fetchone()
        return row[0] if row and row[0] else datetime.date.today()

    def sync_fx_rates(self):
        """Daily HUB_CURRENCY price history for every currency referenced by an asset or
        configured, since the earliest transaction date. The hub itself gets a synthetic,
        always-1.0 history so it can be selected as a benchmark like any other currency."""
        with self.conn.cursor() as cur:
            currencies = self._tracked_currencies(cur)
            asset_ids = {curr: get_or_create_asset(cur, curr, "cash", curr, is_liquid=True) for curr in currencies}
            hub_asset_id = get_or_create_asset(cur, HUB_CURRENCY, "cash", HUB_CURRENCY, is_liquid=True)
            start_date = min(cfg.HISTORY_START_DATE, self._inception_date(cur))
        self.conn.commit()

        tickers = {f"{curr}{HUB_CURRENCY}=X": asset_id for curr, asset_id in asset_ids.items()}
        synced = self._sync_price_history(tickers, label="FX rates")

        today = datetime.date.today()
        hub_records = [(hub_asset_id, start_date + datetime.timedelta(days=n), 1.0)
                       for n in range((today - start_date).days + 1)]
        with self.conn.cursor() as cur:
            upsert_asset_prices(cur, hub_records)
        self.conn.commit()
        print(f"Synced {synced} FX rates + {len(hub_records)} synthetic {HUB_CURRENCY}-identity prices.")

    def _sync_price_history(self, assets: dict, ticker_suffix: str = "", label: str = "prices",
                             fetch_splits: bool = False) -> int:
        """assets: {yfinance_ticker: asset_id}. Fetches daily history since inception and
        upserts into asset_prices. `fetch_splits=True` also un-adjusts each row back to its
        true nominal (as-traded) price and records each split event in asset_splits."""
        if not assets:
            return 0
        with self.conn.cursor() as cur:
            # Start fetching historical prices a bit earlier than the official start date to ensure coverage
            start_date = min(cfg.HISTORY_START_DATE, self._inception_date(cur)) - datetime.timedelta(days=10)
        today = datetime.date.today()

        price_records = []
        split_records = []
        for symbol, asset_id in assets.items():
            ticker_symbol = f"{symbol}{ticker_suffix}"
            try:
                ticker = yf.Ticker(ticker_symbol)
                history = ticker.history(start=start_date, end=today + datetime.timedelta(days=1),
                                          auto_adjust=False)
                splits = []
                if fetch_splits:
                    splits = [(ts.date(), float(ratio)) for ts, ratio in ticker.splits.items()]
                    split_records.extend((asset_id, split_date, ratio) for split_date, ratio in splits)
                for ts, close in history["Close"].items():
                    if not _valid(close):
                        continue
                    d = ts.date()
                    factor = 1.0
                    for split_date, ratio in splits:
                        if split_date > d:
                            factor *= ratio
                    price_records.append((asset_id, d, float(close) * factor))
            except Exception as e:
                print(f"Could not fetch price history for {ticker_symbol}: {e}")

        if price_records:
            with self.conn.cursor() as cur:
                upsert_asset_prices(cur, price_records)
                upsert_asset_splits(cur, split_records)
            self.conn.commit()
        print(f"Synced {len(price_records)} historical {label}"
              + (f" + {len(split_records)} split events" if split_records else "") + ".")
        return len(price_records)

    def sync_equity_prices(self):
        """Daily price history for all liquid equities/ETFs, since portfolio inception. Also
        backfills split ratios - the only asset class where a real stock split can occur."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT symbol, id FROM assets WHERE is_liquid = TRUE AND asset_class = 'equity';")
            assets = dict(cur.fetchall())
        self._sync_price_history(assets, label="equity/ETF prices", fetch_splits=True)

    def sync_crypto_prices(self):
        """Daily USD price history for all liquid crypto assets, since portfolio inception."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT symbol, id FROM assets WHERE is_liquid = TRUE AND asset_class = 'crypto';")
            assets = dict(cur.fetchall())
        self._sync_price_history(assets, ticker_suffix="-USD", label="crypto prices")

    def sync_benchmark_prices(self):
        """Daily price history for every benchmark configured in cfg.BENCHMARKS, since
        portfolio inception. Creates an asset row per benchmark (asset_class 'benchmark'),
        never referenced by a transaction, so it stays invisible to net worth/allocation."""
        with self.conn.cursor() as cur:
            asset_ids = {
                symbol: get_or_create_asset(cur, symbol, "benchmark", info["currency"], is_liquid=True)
                for symbol, info in cfg.BENCHMARKS.items()
            }
        self.conn.commit()
        tickers = {info["ticker"]: asset_ids[symbol] for symbol, info in cfg.BENCHMARKS.items()}
        self._sync_price_history(tickers, label="benchmark prices")
