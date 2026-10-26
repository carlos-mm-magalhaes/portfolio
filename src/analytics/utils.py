import datetime
from decimal import Decimal
from typing import List, Optional, Union
from scipy import optimize
from config import cfg

# All FX rates are stored/looked-up against this fixed hub currency (regardless of the
# configured display/base currency), so any currency can be converted to any other via a single
# extra hop (native -> HUB -> display) instead of needing a rate for every pair.
HUB_CURRENCY = "USD"


def hub_to_currency_rate(cur, currency: str, as_of: datetime.date) -> float:
    """Multiplier to convert a HUB_CURRENCY amount into `currency`, as of `as_of` (1.0 if
    `currency` is already the hub). A currency is just another 'cash' asset whose
    price history *is* its HUB_CURRENCY value."""
    if currency == HUB_CURRENCY:
        return 1.0
    cur.execute("""
        SELECT ap.price FROM asset_prices ap
        JOIN assets a ON a.id = ap.asset_id AND a.asset_class = 'cash'
        WHERE a.symbol = %s AND ap.price_date <= %s
        ORDER BY ap.price_date DESC LIMIT 1;
    """, (currency, as_of))
    row = cur.fetchone()
    native_to_hub = float(row[0]) if row and row[0] else 1.0
    return 1.0 / native_to_hub if native_to_hub else 1.0


def annualize_rate(rate: float, days: int) -> float:
    """Converts a rate earned over `days` into the equivalent fixed-365-day annualized rate
    (CAGR-style): (1+rate)^(365/(days+1)) - 1. `rate` unchanged if `days` isn't positive."""
    if days <= 0:
        return rate
    return (1.0 + rate) ** (365.0 / (days + 1)) - 1.0


def calculate_xirr(
    dates: List[Union[datetime.date, datetime.datetime]],
    flows: List[Union[float, int, Decimal]],
    guess: float = 0.10
) -> Optional[float]:
    """Money-weighted return: solves for the discount rate that makes the NPV of `flows`
    on their own `dates` equal zero (deposit negative/withdrawal positive/terminal positive).
    Returns 0.0 on insufficient or invalid input."""
    if not dates or not flows or len(dates) != len(flows) or len(dates) < 2:
        print("Inconsistent or insufficient data for XIRR calculation.")
        return 0.0

    # 1. Cast ALL flows to float (eliminates the Decimal/float TypeError)
    float_flows = [float(f) for f in flows]

    # Verify at least one positive (inflow/terminal) and one negative (outflow)
    if not (any(f > 0 for f in float_flows) and any(f < 0 for f in float_flows)):
        print("Cash flows must contain at least one positive and one negative value for XIRR calculation.")
        return 0.0

    # 2. Normalize all dates to standard datetime.date
    clean_dates = [
        d.date() if isinstance(d, datetime.datetime) else d
        for d in dates
    ]
    d0 = clean_dates[0]
    dn = clean_dates[-1]
    days = [(d - d0).days for d in clean_dates]

    def npv(r: float) -> float:
        if r <= -0.9999:
            return float("inf")
        return sum(cf / ((1.0 + r) ** (day / (dn - d0).days)) for cf, day in zip(float_flows, days))

    # 1. Bounded Brent solver
    try:
        return float(optimize.brentq(npv, -0.999, 10.0))
    except Exception:
        pass

    # 2. Newton-Raphson fallback
    try:
        return float(optimize.newton(npv, guess, maxiter=200))
    except Exception:
        print("Both methods failed to converge.")
        return 0.0


def fx_series(cur, currency: str, end_date: datetime.date) -> list:
    """[(date, hub->currency rate)] ascending, up to `end_date` - an as-of lookup table so
    converting a long daily series doesn't need one query per date. Empty if `currency`
    is already HUB_CURRENCY."""
    if currency == HUB_CURRENCY:
        return []
    cur.execute("""
        SELECT ap.price_date, ap.price FROM asset_prices ap
        JOIN assets a ON a.id = ap.asset_id AND a.asset_class = 'cash'
        WHERE a.symbol = %s AND ap.price_date <= %s
        ORDER BY ap.price_date;
    """, (currency, end_date))
    return [(r[0], 1.0 / float(r[1])) for r in cur.fetchall() if r[1]]


def apply_fx(rows: list, fx_rates: list) -> list:
    """rows: [(date, value)] ascending. Multiplies each by the rate in effect as of its own
    date (1.0 throughout if `fx_rates` is empty). A `None` value passes through unchanged."""
    if not fx_rates:
        return rows
    result = []
    idx, rate = 0, 1.0
    for date, value in rows:
        while idx < len(fx_rates) and fx_rates[idx][0] <= date:
            rate = fx_rates[idx][1]
            idx += 1
        result.append((date, value * rate if value is not None else None))
    return result


def calculate_chained_twr(value_rows: List[tuple], cash_flows: List[tuple]) -> List[tuple]:
    """Chained time-weighted return series: [(date, cumulative_pct_growth), ...], rebased to
    0% at the first point. Backs out each step's net external flow before compounding, so the
    result ignores any money in/out."""
    if not value_rows:
        return []
    contributions_by_date = {}
    for flow_date, amount in cash_flows:
        contributions_by_date[flow_date] = contributions_by_date.get(flow_date, 0.0) - amount

    points = [(value_rows[0][0], 0.0)]
    growth_factor = 1.0
    for (_, prev_value), (curr_date, curr_value) in zip(value_rows, value_rows[1:]):
        if prev_value:
            contribution = contributions_by_date.get(curr_date, 0.0)
            growth_factor *= (curr_value - contribution) / prev_value
        points.append((curr_date, round((growth_factor - 1) * 100, 2)))
    return points


def named_period_windows(as_of: datetime.date, inception_date: datetime.date) -> dict:
    """Standard reporting periods: YTD, one row per full calendar year since
    cfg.HISTORY_START_DATE, and all-time. Returns {label: (start, end)} - `all_time`'s start
    is the day before `inception_date`, always a concrete date rather than `None`."""
    periods = {
        "all_time": (inception_date - datetime.timedelta(days=1), as_of),
        "ytd": (datetime.date(as_of.year, 1, 1), as_of)
    }
    for y in range(as_of.year - 1, cfg.HISTORY_START_DATE.year - 1, -1):
        periods[f"cy_{y}"] = (datetime.date(y, 1, 1), datetime.date(y, 12, 31))
    return periods


def get_daily_values(cur, start_date: datetime.date, end_date: datetime.date,
                      asset_filter: list = None, account_filter: list = None) -> list:
    """[(date, HUB_CURRENCY value), ...] ascending, one row per calendar day in
    [start_date, end_date]. Every day is present (0 if the filtered scope held nothing that
    day) rather than omitted, so a real discontinuity isn't chart-interpolated over."""
    conditions = []
    join_params = []
    if asset_filter:
        conditions.append("asset_id = ANY(%s)")
        join_params.append(asset_filter)
    if account_filter:
        conditions.append("account_id = ANY(%s)")
        join_params.append(account_filter)
    join_extra = (" AND " + " AND ".join(conditions)) if conditions else ""

    cur.execute(f"""
        WITH days AS (
            SELECT generate_series(%s::date, %s::date, interval '1 day')::date AS d
        )
        SELECT days.d, COALESCE(SUM(s.market_value_base), 0)
        FROM days
        LEFT JOIN daily_portfolio_snapshots s
            ON s.snapshot_date = days.d{join_extra}
        GROUP BY days.d ORDER BY days.d;
    """, [start_date, end_date] + join_params)
    return [(r[0], float(r[1])) for r in cur.fetchall()]


def get_daily_price_series(cur, asset_id: int, start_date: datetime.date, end_date: datetime.date) -> list:
    """[(date, price|None), ...] ascending, one row per calendar day, forward-filled from the
    latest known price on or before that day (`None` before the asset's first recorded price).
    Re-adjusted by dividing by the cumulative ratio of every split after that day, so a chart doesn't show a fake cliff on the split date."""
    cur.execute("""
        WITH days AS (
            SELECT generate_series(%s::date, %s::date, interval '1 day')::date AS d
        )
        SELECT days.d, price_pit.price / COALESCE(sr.factor, 1.0)
        FROM days
        LEFT JOIN LATERAL (
            SELECT ap.price
            FROM asset_prices ap
            WHERE ap.asset_id = %s AND ap.price_date <= days.d
            ORDER BY ap.price_date DESC
            LIMIT 1
        ) price_pit ON TRUE
        LEFT JOIN LATERAL (
            SELECT EXP(SUM(LN(s.ratio))) AS factor
            FROM asset_splits s
            WHERE s.asset_id = %s AND s.split_date > days.d
        ) sr ON TRUE
        ORDER BY days.d;
    """, (start_date, end_date, asset_id, asset_id))
    return [(r[0], float(r[1]) if r[1] is not None else None) for r in cur.fetchall()]


def get_external_cash_flows(cur, start_date: datetime.date = None, end_date: datetime.date = None,
                             asset_filter: list = None, account_filter: list = None, subset_mode: bool = True):
    """[(date, HUB_CURRENCY signed flow, description), ...], converted at the FX rate in
    effect on each transaction's own date (`start_date` exclusive, `end_date` inclusive).
    Without `asset_filter`/`subset_mode`, only DEPOSIT/WITHDRAWAL count (a container scope,
    where BUY/SELL legs cancel internally); with both, BUY/SELL of that subset count too."""
    conditions = []
    params = []

    if asset_filter:
        conditions.append("t.asset_id = ANY(%s)")
        params.append(asset_filter)

    if account_filter:
        conditions.append("t.account_id = ANY(%s)")
        params.append(account_filter)

    if asset_filter and subset_mode:
        tx_types = "('DEPOSIT', 'WITHDRAWAL', 'BUY', 'SELL')"
        sign_case = """
            CASE
                WHEN t.tx_type IN ('DEPOSIT', 'BUY') THEN -native_amount.hub_value
                WHEN t.tx_type IN ('WITHDRAWAL', 'SELL') THEN native_amount.hub_value
            END
        """
    else:
        tx_types = "('DEPOSIT', 'WITHDRAWAL')"
        sign_case = """
            CASE
                WHEN t.tx_type = 'DEPOSIT' THEN -native_amount.hub_value
                WHEN t.tx_type = 'WITHDRAWAL' THEN native_amount.hub_value
            END
        """

    if start_date:
        conditions.append("t.executed_at::date > %s")
        params.append(start_date)
    if end_date:
        conditions.append("t.executed_at::date <= %s")
        params.append(end_date)

    extra_where = (" AND " + " AND ".join(conditions)) if conditions else ""

    cur.execute(f"""
        SELECT
            t.executed_at::date AS flow_date,
            {sign_case} AS cash_flow,
            t.tx_type, t.quantity, native_amount.native_value, a.currency AS native_currency, a.symbol
        FROM transactions t
        JOIN assets a ON t.asset_id = a.id
        LEFT JOIN LATERAL (
            SELECT ap.price
            FROM asset_prices ap
            WHERE ap.asset_id = t.asset_id AND ap.price_date <= t.executed_at::date
            ORDER BY ap.price_date DESC
            LIMIT 1
        ) price_pit ON a.asset_class != 'cash'
        LEFT JOIN LATERAL (
            SELECT ap.price AS rate
            FROM asset_prices ap
            JOIN assets ca ON ca.id = ap.asset_id AND ca.asset_class = 'cash'
            WHERE ca.symbol = a.currency AND ap.price_date <= t.executed_at::date
            ORDER BY ap.price_date DESC
            LIMIT 1
        ) fx_pit ON a.currency != %s
        CROSS JOIN LATERAL (
            SELECT
                ABS(t.quantity) * (CASE WHEN a.asset_class = 'cash' THEN 1.0 ELSE COALESCE(price_pit.price, 0.0) END) AS native_value,
                ABS(t.quantity) * (CASE WHEN a.asset_class = 'cash' THEN 1.0 ELSE COALESCE(price_pit.price, 0.0) END)
                    * COALESCE(fx_pit.rate, 1.0) AS hub_value
        ) native_amount
        WHERE t.tx_type IN {tx_types}
        {extra_where}
        ORDER BY flow_date ASC, t.id ASC;
    """, [HUB_CURRENCY] + params)

    flows = []
    for flow_date, cash_flow, tx_type, quantity, native_value, native_currency, symbol in cur.fetchall():
        description = (
            f"{tx_type} {symbol}: qty={float(quantity):+.4f}, "
            f"amount={float(native_value):,.2f} {native_currency}"
        )
        flows.append((flow_date, float(cash_flow), description))
    return flows
