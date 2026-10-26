import datetime
from config import cfg, ASSET_CLASS_LABELS
from analytics.utils import (annualize_rate, apply_fx, calculate_chained_twr, calculate_xirr, fx_series,
                             get_daily_values, get_external_cash_flows, hub_to_currency_rate,
                             named_period_windows)


class AnalyticsService:
    def __init__(self, db_conn):
        self.conn = db_conn

    def _hub_to_currency_rate(self, cur, currency: str, as_of: datetime.date) -> float:
        return hub_to_currency_rate(cur, currency, as_of)

    def _resolve_asset_filter(self, cur, asset_class: str = None, asset_ids: list = None):
        """Returns a list of asset ids to scope the analysis to, or None for no filter."""
        if asset_ids:
            return list(asset_ids)
        if asset_class:
            cur.execute("SELECT id FROM assets WHERE asset_class = %s;", (asset_class,))
            return [r[0] for r in cur.fetchall()]
        return None

    def _get_snapshot_value(self, cur, snapshot_date: datetime.date, asset_filter: list = None,
                             account_filter: list = None) -> float:
        conditions = []
        params = [snapshot_date]
        if asset_filter:
            conditions.append("asset_id = ANY(%s)")
            params.append(asset_filter)
        if account_filter:
            conditions.append("account_id = ANY(%s)")
            params.append(account_filter)
        extra_where = (" AND " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"""
            SELECT COALESCE(SUM(market_value_base), 0) FROM daily_portfolio_snapshots
            WHERE snapshot_date = %s{extra_where};
        """, params)
        return float(cur.fetchone()[0])

    def _get_allocation(self, cur, snapshot_date: datetime.date, currency: str, asset_filter: list = None,
                         account_filter: list = None) -> list:
        """Asset-class breakdown as of `snapshot_date`, converted to `currency` at the FX rate
        in effect on that date."""
        conditions = []
        params = [snapshot_date]
        if asset_filter:
            conditions.append("s.asset_id = ANY(%s)")
            params.append(asset_filter)
        if account_filter:
            conditions.append("s.account_id = ANY(%s)")
            params.append(account_filter)
        extra_where = (" AND " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"""
            SELECT a.asset_class, SUM(s.market_value_base) AS total_hub
            FROM daily_portfolio_snapshots s
            JOIN assets a ON s.asset_id = a.id
            WHERE s.snapshot_date = %s{extra_where}
            GROUP BY a.asset_class
            ORDER BY total_hub DESC;
        """, params)
        rows = cur.fetchall()
        total_hub = sum(r[1] for r in rows) if rows else 0.0
        fx_rate = self._hub_to_currency_rate(cur, currency, snapshot_date)

        return [
            {
                "class": ASSET_CLASS_LABELS.get(r[0], r[0]),
                "value": round(float(r[1]) * fx_rate, cfg.PRECISION),
                "weight_pct": round((float(r[1]) / float(total_hub)) * 100, cfg.PRECISION) if total_hub > 0 else 0.0
            }
            for r in rows
        ]

    def _get_snapshot_rows(self, cur, snapshot_date: datetime.date, asset_filter: list = None,
                            account_filter: list = None) -> list:
        """Raw (account_id, account_name, symbol, quantity, market_value_base) rows as of
        `snapshot_date` - shared by every per-asset balance breakdown below."""
        conditions = []
        params = [snapshot_date]
        if asset_filter:
            conditions.append("s.asset_id = ANY(%s)")
            params.append(asset_filter)
        if account_filter:
            conditions.append("s.account_id = ANY(%s)")
            params.append(account_filter)
        extra_where = (" AND " + " AND ".join(conditions)) if conditions else ""

        cur.execute(f"""
            SELECT acc.id, acc.name, a.symbol, s.quantity_held, s.market_value_base
            FROM daily_portfolio_snapshots s
            JOIN accounts acc ON s.account_id = acc.id
            JOIN assets a ON s.asset_id = a.id
            WHERE s.snapshot_date = %s{extra_where}
            ORDER BY acc.name, a.symbol;
        """, params)
        return cur.fetchall()

    def _get_asset_balances(self, cur, snapshot_date: datetime.date, currency: str, asset_filter: list = None,
                            account_filter: list = None) -> list:
        """Per-asset balance breakdown as of `snapshot_date`, converted to `currency`. Assets
        held across multiple accounts within scope are summed by symbol."""
        rows = self._get_snapshot_rows(cur, snapshot_date, asset_filter, account_filter)
        fx_rate = self._hub_to_currency_rate(cur, currency, snapshot_date)

        totals_by_symbol = {}
        for _, _, symbol, _, value_hub in rows:
            totals_by_symbol[symbol] = totals_by_symbol.get(symbol, 0.0) + float(value_hub)

        return [{"symbol": symbol, "value": round(total * fx_rate, cfg.PRECISION)}
                for symbol, total in sorted(totals_by_symbol.items())]

    def get_period_report(self, period_start: datetime.date, period_end: datetime.date, currency: str,
                           account_id: int = None, asset_class: str = None, asset_ids: list = None) -> dict:
        """Standard period report in `currency`: start/end balance, allocation, cash flows, and
        XIRR/TWR, each converted at the FX rate in effect on its own date. `asset_class`/
        `asset_ids` narrow BUY/SELL to external flows, unless `asset_class == "cash"`."""

        account_filter = [account_id] if account_id else None
        subset_mode = asset_class != "cash"

        with self.conn.cursor() as cur:
            asset_filter = self._resolve_asset_filter(cur, asset_class, asset_ids)
            flow_rows_hub = get_external_cash_flows(cur, start_date=period_start, end_date=period_end,
                                                     asset_filter=asset_filter, account_filter=account_filter,
                                                     subset_mode=subset_mode)
            end_value_hub = self._get_snapshot_value(cur, period_end, asset_filter, account_filter)
            end_value = end_value_hub * self._hub_to_currency_rate(cur, currency, period_end)
            end_allocation = self._get_allocation(cur, period_end, currency, asset_filter, account_filter)
            end_assets = self._get_asset_balances(cur, period_end, currency, asset_filter, account_filter)

            start_value_hub = self._get_snapshot_value(cur, period_start, asset_filter, account_filter)
            start_value = start_value_hub * self._hub_to_currency_rate(cur, currency, period_start)
            start_allocation = self._get_allocation(cur, period_start, currency, asset_filter, account_filter)
            start_assets = self._get_asset_balances(cur, period_start, currency, asset_filter, account_filter)
            xirr_dates = [period_start]
            xirr_flows = [-start_value]

            cash_flows = []
            for flow_date, flow_hub, description in flow_rows_hub:
                flow_display = flow_hub * self._hub_to_currency_rate(cur, currency, flow_date)
                cash_flows.append({"date": flow_date, "amount": round(flow_display, cfg.PRECISION),
                                    "description": description})
                xirr_dates.append(flow_date)
                xirr_flows.append(flow_display)

        xirr_dates.append(period_end)
        xirr_flows.append(end_value)
        xirr = calculate_xirr(xirr_dates, xirr_flows, guess=cfg.XIRR_DEFAULT_GUESS)

        with self.conn.cursor() as cur:
            daily_values_hub = get_daily_values(cur, period_start, period_end, asset_filter, account_filter)
            rates = fx_series(cur, currency, period_end)
        daily_values = apply_fx(daily_values_hub, rates)

        twr_series = calculate_chained_twr(daily_values, [(cf["date"], cf["amount"]) for cf in cash_flows])
        twr = (twr_series[-1][1] / 100.0) if len(twr_series) > 1 else None

        return {
            "currency": currency,
            "start_date": period_start,
            "end_date": period_end,
            "start_value": round(start_value, cfg.PRECISION),
            "start_allocation": start_allocation,
            "start_assets": start_assets,
            "cash_flows": cash_flows,
            "end_value": round(end_value, cfg.PRECISION),
            "end_allocation": end_allocation,
            "end_assets": end_assets,
            "xirr_pct": round(xirr * 100, cfg.PRECISION),
            "twr_pct": round(twr * 100, cfg.PRECISION) if twr is not None else None,
        }

    def list_scopes(self) -> list:
        """{id, name, assets: [{id, symbol}]} list per account, without computing any reports.
        Cash is excluded: it's a container bucket, not something to drill into."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, name FROM accounts ORDER BY name;")
            accounts = cur.fetchall()

            scopes = []
            for account_id, account_name in accounts:
                cur.execute("""
                    SELECT DISTINCT a.id, a.symbol FROM transactions t
                    JOIN assets a ON t.asset_id = a.id
                    WHERE t.account_id = %s AND a.asset_class != 'cash' ORDER BY a.symbol;
                """, (account_id,))
                assets = [{"id": r[0], "symbol": r[1]} for r in cur.fetchall()]
                scopes.append({"id": account_id, "name": account_name, "assets": assets})
        return scopes

    def get_asset_balances_by_account(self, snapshot_date: datetime.date, currencies: list,
                                       account_id: int = None, asset_ids: list = None) -> dict:
        """Per-account, per-asset balance breakdown (with quantity held) as of `snapshot_date`,
        one value column per currency, plus per-account and grand totals."""
        account_filter = [account_id] if account_id else None
        with self.conn.cursor() as cur:
            rows = self._get_snapshot_rows(cur, snapshot_date, asset_ids, account_filter)
            fx_rates = {currency: self._hub_to_currency_rate(cur, currency, snapshot_date)
                        for currency in currencies}

        accounts = {}
        totals = {currency: 0.0 for currency in currencies}
        for acc_id, account_name, symbol, quantity, value_hub in rows:
            account = accounts.setdefault(acc_id, {"name": account_name, "assets": [],
                                                     "subtotal": {c: 0.0 for c in currencies}})
            values = {currency: float(value_hub) * rate for currency, rate in fx_rates.items()}
            account["assets"].append({"symbol": symbol, "quantity": float(quantity),
                                       "amounts": {c: round(v, cfg.PRECISION) for c, v in values.items()}})
            for currency, value in values.items():
                account["subtotal"][currency] += value
                totals[currency] += value

        return {
            "accounts": [
                {"name": acc["name"], "assets": acc["assets"],
                 "subtotal": {c: round(v, cfg.PRECISION) for c, v in acc["subtotal"].items()}}
                for acc in accounts.values()
            ],
            "totals": {c: round(v, cfg.PRECISION) for c, v in totals.items()},
        }

    def get_inception_date(self) -> datetime.date:
        """Earliest transaction date across the whole portfolio."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT MIN(executed_at)::date FROM transactions;")
            return cur.fetchone()[0]

    def get_period_cash_flows(self, period_start: datetime.date, period_end: datetime.date,
                               account_id: int = None, asset_class: str = None, asset_ids: list = None) -> tuple:
        """(dates, HUB_CURRENCY amounts) cash-flow series for XIRR, in raw hub currency without
        the allocation/asset-balance/TWR work. `period_start` is always a concrete date."""
        account_filter = [account_id] if account_id else None
        subset_mode = asset_class != "cash"
        with self.conn.cursor() as cur:
            asset_filter = self._resolve_asset_filter(cur, asset_class, asset_ids)
            flow_rows_hub = get_external_cash_flows(cur, start_date=period_start, end_date=period_end,
                                                     asset_filter=asset_filter, account_filter=account_filter,
                                                     subset_mode=subset_mode)
            end_value_hub = self._get_snapshot_value(cur, period_end, asset_filter, account_filter)
            start_value_hub = self._get_snapshot_value(cur, period_start, asset_filter, account_filter)
            dates = [period_start]
            flows = [-start_value_hub]
            for flow_date, flow_hub, _ in flow_rows_hub:
                dates.append(flow_date)
                flows.append(flow_hub)
        dates.append(period_end)
        flows.append(end_value_hub)
        return dates, flows

    def xirr_from_hub_flows(self, dates: list, hub_flows: list, currency: str, annualize: bool = False) -> float:
        """Converts a hub-currency cash-flow series into `currency` and solves XIRR.
        `annualize=True` converts the result into a CAGR-style annual rate."""
        with self.conn.cursor() as cur:
            display_flows = [amount * hub_to_currency_rate(cur, currency, date)
                              for date, amount in zip(dates, hub_flows)]
        rate = calculate_xirr(dates, display_flows, guess=cfg.XIRR_DEFAULT_GUESS)
        if annualize:
            rate = annualize_rate(rate, (dates[-1] - dates[0]).days)
        return round(rate * 100, cfg.PRECISION)

    def get_named_period_reports(self, currency: str, as_of: datetime.date = None, account_id: int = None,
                                  asset_class: str = None, asset_ids: list = None) -> dict:
        """Period report for a standard set of reporting periods (YTD, full calendar years,
        trailing windows, and all-time), for one currency. Returns {period_label: report}."""
        as_of = as_of or datetime.date.today()
        periods = named_period_windows(as_of, self.get_inception_date())

        return {
            label: self.get_period_report(start, end, currency, account_id=account_id,
                                           asset_class=asset_class, asset_ids=asset_ids)
            for label, (start, end) in periods.items()
        }
