import datetime
from config import cfg
from analytics.utils import (HUB_CURRENCY, apply_fx, calculate_chained_twr, fx_series,
                             get_daily_price_series, get_daily_values, get_external_cash_flows)


class EvolutionService:
    """Portfolio value over time vs. cumulative net invested, plus rebased %-growth series
    for benchmark comparison."""

    def __init__(self, db_conn):
        self.conn = db_conn

    @staticmethod
    def _to_hub_series(cur, native_currency: str, end_date: datetime.date) -> list:
        """[(date, native->hub rate)] ascending. A currency is just another 'cash' asset
        whose price history *is* its HUB_CURRENCY value."""
        if native_currency == HUB_CURRENCY:
            return []
        cur.execute("""
            SELECT ap.price_date, ap.price FROM asset_prices ap
            JOIN assets a ON a.id = ap.asset_id AND a.asset_class = 'cash'
            WHERE a.symbol = %s AND ap.price_date <= %s
            ORDER BY ap.price_date;
        """, (native_currency, end_date))
        return [(r[0], float(r[1])) for r in cur.fetchall() if r[1]]

    def _convert_series(self, cur, series: list, native_currency: str, target_currency: str,
                         end_date: datetime.date) -> list:
        """Converts `series` into `target_currency`, bridging through the hub currency when
        neither side already is it, since only hub-denominated rates are stored."""
        if native_currency == target_currency:
            return series
        if native_currency != HUB_CURRENCY:
            series = apply_fx(series, self._to_hub_series(cur, native_currency, end_date))
        if target_currency != HUB_CURRENCY:
            series = apply_fx(series, fx_series(cur, target_currency, end_date))
        return series

    def get_evolution(self, currency: str, period_start: datetime.date, period_end: datetime.date,
                       account_id: int = None, asset_ids: list = None) -> list:
        """[{date, value, net_invested}, ...] in `currency`, one point per day in
        [period_start, period_end]. Net Invested is a running total of external contributions
        (a deposit increases it), forward-filled onto the value series' own dates."""
        asset_filter = list(asset_ids) if asset_ids else None
        account_filter = [account_id] if account_id else None

        with self.conn.cursor() as cur:
            value_rows_hub = get_daily_values(cur, period_start, period_end, asset_filter, account_filter)
            flow_rows_hub = get_external_cash_flows(cur, end_date=period_end,
                                                     asset_filter=asset_filter, account_filter=account_filter)
            rates = fx_series(cur, currency, period_end)

        value_rows = apply_fx(value_rows_hub, rates)
        flow_rows = apply_fx([(d, amount) for d, amount, _ in flow_rows_hub], rates)

        points = []
        flow_idx, running_total = 0, 0.0
        for date, value in value_rows:
            while flow_idx < len(flow_rows) and flow_rows[flow_idx][0] <= date:
                running_total += -flow_rows[flow_idx][1]  # XIRR sign -> "contribution" sign
                flow_idx += 1
            points.append({"date": date, "value": round(value, cfg.PRECISION),
                            "net_invested": round(running_total, cfg.PRECISION)})
        return points

    def _benchmark_native_series(self, cur, benchmark_key: str, period_start: datetime.date,
                                  period_end: datetime.date):
        """Returns (native_currency, [(date, price|None)]) for one benchmark - a tracked
        currency, configured index, or portfolio asset. Forward-filled to one entry per
        calendar day so it lines up with the portfolio's own daily series."""
        cur.execute("SELECT id, currency, asset_class FROM assets WHERE symbol = %s;", (benchmark_key,))
        row = cur.fetchone()
        if not row:
            return None, []
        asset_id, currency, asset_class = row
        native_currency = HUB_CURRENCY if asset_class == "cash" else currency
        return native_currency, get_daily_price_series(cur, asset_id, period_start, period_end)

    def get_growth_comparison(self, currency: str, period_start: datetime.date, period_end: datetime.date,
                               benchmark_keys: list, account_id: int = None, asset_ids: list = None) -> dict:
        """Returns {"portfolio": [(date, pct)], "benchmarks": {key: [(date, pct)]}}, all
        converted into `currency` first. The portfolio side is a chained TWR, not a rebased
        raw value, so it isn't distorted by deposit/withdrawal size like a rebased price series."""
        benchmark_series = {}
        with self.conn.cursor() as cur:
            portfolio_growth = self._chained_twr_growth(cur, currency, period_start, period_end,
                                                         account_id, asset_ids)
            for key in benchmark_keys:
                native_currency, series = self._benchmark_native_series(cur, key, period_start, period_end)
                if not any(value is not None for _, value in series):
                    continue
                series = self._convert_series(cur, series, native_currency, currency, period_end)
                benchmark_series[key] = series

        return {
            "portfolio": portfolio_growth,
            "benchmarks": {key: self._rebase(series) for key, series in benchmark_series.items()},
        }

    def _chained_twr_growth(self, cur, currency: str, period_start: datetime.date, period_end: datetime.date,
                             account_id: int = None, asset_ids: list = None) -> list:
        """[(date, pct)] cumulative time-weighted return, rebased to 0% at `period_start`.
        Unlike raw portfolio value, isn't inflated/deflated by deposit/withdrawal size."""
        asset_filter = list(asset_ids) if asset_ids else None
        account_filter = [account_id] if account_id else None

        value_rows_hub = get_daily_values(cur, period_start, period_end, asset_filter, account_filter)
        if not value_rows_hub:
            return []

        flow_rows_hub = get_external_cash_flows(cur, start_date=period_start, end_date=period_end,
                                                 asset_filter=asset_filter, account_filter=account_filter)
        rates = fx_series(cur, currency, period_end)
        value_rows = apply_fx(value_rows_hub, rates)
        flow_rows = apply_fx([(d, amount) for d, amount, _ in flow_rows_hub], rates)
        return calculate_chained_twr(value_rows, flow_rows)

    @staticmethod
    def _rebase(series: list) -> list:
        """[(date, value|None)] -> [(date, pct_growth_from_first_known_value|None)] - leading
        `None`s (before a benchmark's first recorded price) stay `None` rather than crashing
        on a missing base."""
        base = next((v for _, v in series if v is not None), None)
        if not base:
            return [(d, None if v is None else 0.0) for d, v in series]
        return [(d, None if v is None else round((v / base - 1) * 100, 2)) for d, v in series]
