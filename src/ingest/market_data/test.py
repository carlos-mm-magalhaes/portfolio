import datetime
import math
import yfinance as yf


def _valid(value) -> bool:
    """True for a real, usable price/rate - filters out None, 0, and NaN (yfinance/pandas can
    return NaN for a data hiccup, which is truthy in Python and would otherwise slip through)."""
    return bool(value) and not math.isnan(value)


class YahooMarketData:

    def sync_price(self, start_date: datetime.date, end_date: datetime.date,
                   assets: dict, ticker_suffix: str = "", label: str = "prices",
                   fetch_splits: bool = False) -> int:

        price_records = []
        for symbol, asset_id in assets.items():
            ticker_symbol = f"{symbol}{ticker_suffix}"
            try:
                ticker = yf.Ticker(ticker_symbol)
                history = ticker.history(start=start_date, end=end_date,
                                         auto_adjust=False)
                # Fetch all lifetime splits sorted chronologically: [(date, ratio), ...]
                sorted_splits = []
                if fetch_splits and ticker.splits is not None and not ticker.splits.empty:
                    sorted_splits = sorted(
                        [(ts.date(), float(ratio)) for ts, ratio in ticker.splits.items()],
                        key=lambda x: x[0]
                    )
                print(f"Splits for {symbol}: {dict(sorted_splits)}")  # Debug: print splits for each symbol
                for ts, row in history.iterrows():
                    close = row["Close"]
                    if not _valid(close):
                        continue

                    d = ts.date()
                    close_val = float(close)

                    # Cumulative split factor = product of all splits executed on or before this date
                    cumulative_split_ratio = 1.0
                    if fetch_splits:
                        for split_date, ratio in sorted_splits:
                            if split_date > d:
                                cumulative_split_ratio *= ratio
                    price_records.append((symbol, d, close_val, cumulative_split_ratio))

            except Exception as e:
                print(f"Could not fetch price history for {ticker_symbol}: {e}")

        print(f"Synced {len(price_records)} historical {label}.")
        for symbol, date, close, split_ratio in price_records:
            print(f"Symbol: {symbol}, Date: {date}, Close: {close}, Split Ratio: {split_ratio}")
        return len(price_records)


if __name__ == "__main__":
    yahoo = YahooMarketData()
    assets = {
        "OGI": 1,
        "NOBL": 2,
        "NVDA": 3,
    }
    start_date = datetime.date(2021, 5, 6)
    end_date = datetime.date(2021, 5, 7)
    yahoo.sync_price(start_date=start_date, end_date=end_date, assets=assets, fetch_splits=True)

# Result of test script:

# $ python test.py
# Splits for OGI: {datetime.date(2023, 7, 7): 0.25}
# Splits for NOBL: {datetime.date(2026, 5, 28): 2.0}
# Splits for NVDA: {datetime.date(2000, 6, 27): 2.0, datetime.date(2001, 9, 12): 2.0, datetime.date(2006,4, 7): 2.0, datetime.date(2007, 9, 11): 1.5, datetime.date(2021, 7, 20): 4.0, datetime.date(2024, 6, 10): 10.0}
# Synced 12 historical prices.
# Symbol: OGI, Date: 2021-07-26, Close: 10.319999694824219, Split Ratio: 1.0
# Symbol: OGI, Date: 2021-07-27, Close: 9.84000015258789, Split Ratio: 1.0
# Symbol: OGI, Date: 2021-07-28, Close: 10.920000076293945, Split Ratio: 1.0
# Symbol: OGI, Date: 2021-07-29, Close: 10.600000381469727, Split Ratio: 1.0
# Symbol: NOBL, Date: 2021-07-26, Close: 41.49176788330078, Split Ratio: 1.0
# Symbol: NOBL, Date: 2021-07-27, Close: 41.5502815246582, Split Ratio: 1.0
# Symbol: NOBL, Date: 2021-07-28, Close: 41.37471008300781, Split Ratio: 1.0
# Symbol: NOBL, Date: 2021-07-29, Close: 41.8204231262207, Split Ratio: 1.0
# Symbol: NVDA, Date: 2021-07-26, Close: 19.206005096435547, Split Ratio: 1.0
# Symbol: NVDA, Date: 2021-07-27, Close: 19.12039566040039, Split Ratio: 1.0
# Symbol: NVDA, Date: 2021-07-28, Close: 19.414045333862305, Split Ratio: 1.0
# Symbol: NVDA, Date: 2021-07-29, Close: 19.57232666015625, Split Ratio: 1.0

# Key FactsTotal impact: One share purchased before June 2000 equals 480 shares today.
