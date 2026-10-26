import datetime

import psycopg2
from dateutil.relativedelta import relativedelta
from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import BASE_DIR, cfg
from analytics.analytics import AnalyticsService
from analytics.evolution import EvolutionService
from analytics.exposure import ExposureService
from analytics.utils import annualize_rate, named_period_windows
from ingest.snapshot import SnapshotService

app = FastAPI(title="Portfolio Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def get_db_connection():
    conn = psycopg2.connect(**cfg.get_db_connection_params())
    try:
        yield conn
    finally:
        conn.close()


def _ensure_today_snapshot(conn):
    """Generates today's snapshot on demand if the nightly cron hasn't run yet (e.g. viewed
    just after midnight) - idempotent and cheap, using whatever prices/FX are already known."""
    today = datetime.date.today()
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM daily_portfolio_snapshots WHERE snapshot_date = %s LIMIT 1;", (today,))
        exists = cur.fetchone() is not None
    if not exists:
        SnapshotService(conn).generate_snapshot(today)


def _period_display_name(label: str) -> str:
    if label == "all_time":
        return "All-Time"
    if label == "ytd":
        return "YTD"
    if label == "custom":
        return "Custom"
    return label.removeprefix("cy_")


def _available_benchmarks(scopes: list, display_currency: str) -> list:
    """Flat {key, label} list for the benchmark selector: tracked currencies, configured
    external indices, and every portfolio asset. A currency's label is `EUR/USD`-style,
    paired against whatever currency the page is currently displayed in."""
    options = [{"key": c, "label": f"{c}/{display_currency}"} for c in cfg.TRACKED_CURRENCIES]
    options += [{"key": key, "label": info["label"]} for key, info in cfg.BENCHMARKS.items()]
    seen = {opt["key"] for opt in options}
    for account in scopes:
        for asset in account["assets"]:
            if asset["symbol"] not in seen:
                options.append({"key": asset["symbol"], "label": asset["symbol"]})
                seen.add(asset["symbol"])
    return options


def _series_to_json(series: list) -> list:
    """[(date, value)] -> [{"date": iso, "value": v}], ready for the template's `| tojson`."""
    return [{"date": d.isoformat(), "value": v} for d, v in series]


def _resolve_scope(scope: str, scopes: list):
    """Parses a "portfolio" / "account:<id>" / "asset:<account_id>:<asset_id>" scope string
    into (account_id, asset_ids, label). An asset scope is qualified by its account id too,
    since the same symbol can be held in more than one account."""
    if not scope or scope == "portfolio":
        return None, None, "Portfolio (All)"

    kind, _, rest = scope.partition(":")

    if kind == "account":
        scope_id = int(rest) if rest.isdigit() else None
        for account in scopes:
            if account["id"] == scope_id:
                return scope_id, None, f"Account: {account['name']}"
    elif kind == "asset":
        raw_account_id, _, raw_asset_id = rest.partition(":")
        account_id = int(raw_account_id) if raw_account_id.isdigit() else None
        asset_id = int(raw_asset_id) if raw_asset_id.isdigit() else None
        for account in scopes:
            if account["id"] != account_id:
                continue
            for asset in account["assets"]:
                if asset["id"] == asset_id:
                    return account_id, [asset_id], f"Account: {account['name']} \u203a {asset['symbol']}"

    return None, None, "Portfolio (All)"


def _build_chart_data(report: dict, chart_start: datetime.date) -> dict:
    """Categorical x-axis (one slot per month, plus leading/trailing blank slots for
    Start/End Value) so every bar centers evenly. Flows are bucketed by month; Start/End
    Value are clamped to +/-2x the largest monthly bucket for display, real amount in tooltip."""
    months = []
    cursor = chart_start.replace(day=1)
    last_month = report["end_date"].replace(day=1)
    while cursor <= last_month:
        months.append(cursor)
        cursor += relativedelta(months=1)

    grouped = {}
    for flow in report["cash_flows"]:
        entry = grouped.setdefault(flow["date"].replace(day=1), {"amount": 0.0, "descriptions": []})
        entry["amount"] += flow["amount"]
        entry["descriptions"].append(f"{flow['date'].isoformat()}: {flow['description']}")

    max_flow_abs = max((abs(v["amount"]) for v in grouped.values()), default=0)
    cap = 2 * max_flow_abs if max_flow_abs else None

    def _display(amount: float):
        if cap and abs(amount) > cap:
            return (cap if amount > 0 else -cap), True
        return amount, False

    labels = [""] + [m.strftime("%b %Y") for m in months] + [""]
    values = [None]
    meta = [None]
    for m in months:
        bucket = grouped.get(m)
        if bucket:
            amount = round(bucket["amount"], 2)
            display, capped = _display(amount)
            values.append(display)
            meta.append({"amount": amount, "descriptions": bucket["descriptions"], "capped": capped, "kind": "flow"})
        else:
            values.append(None)
            meta.append(None)
    values.append(None)
    meta.append(None)

    if report["start_value"] is not None:
        amount = round(-report["start_value"], 2)
        display, capped = _display(amount)
        values[0] = display
        meta[0] = {"amount": amount, "descriptions": [f"{report['start_date'].isoformat()}: Start Value"],
                   "capped": capped, "kind": "boundary"}
    end_amount = round(report["end_value"], 2)
    end_display, end_capped = _display(end_amount)
    values[-1] = end_display
    meta[-1] = {"amount": end_amount, "descriptions": [f"{report['end_date'].isoformat()}: End Value"],
                "capped": end_capped, "kind": "boundary"}

    return {"labels": labels, "values": values, "meta": meta}


@app.get("/", response_class=HTMLResponse)
def view_dashboard(request: Request, currency: str = None, conn=Depends(get_db_connection)):
    _ensure_today_snapshot(conn)
    analytics = AnalyticsService(conn)
    selected_currency = currency if currency in cfg.TRACKED_CURRENCIES else cfg.TRACKED_CURRENCIES[0]
    reports = analytics.get_named_period_reports(selected_currency)

    windows = named_period_windows(datetime.date.today(), analytics.get_inception_date())
    periods = [(label, _period_display_name(label)) for label in windows.keys()]

    # "per" = the row's own window (mirrors reports[label]["xirr_pct"]); "acc" = since
    # inception through the row's end date - both currency-independent (raw HUB_CURRENCY),
    # built once per period for the selected currency's XIRR (acc) column.
    period_flows = {
        label: {"per": analytics.get_period_cash_flows(start, end),
                "acc": analytics.get_period_cash_flows(windows["all_time"][0], end)}
        for label, (start, end) in windows.items()
    }

    for label, flows in period_flows.items():
        reports[label]["xirr_acc_pct"] = analytics.xirr_from_hub_flows(*flows["acc"], selected_currency)

    # KPI cards (Net Worth + annualized XIRR since inception) always show every tracked
    # currency - cheap (one all_time report each), independent of the currency selector that
    # scopes the expensive many-period "Returns by Period" table above to just one currency.
    all_time_reports = {selected_currency: reports["all_time"]}
    for c in cfg.TRACKED_CURRENCIES:
        if c != selected_currency:
            all_time_reports[c] = analytics.get_period_report(*windows["all_time"], c)

    annualized = {
        c: analytics.xirr_from_hub_flows(*period_flows["all_time"]["acc"], c, annualize=True)
        for c in cfg.TRACKED_CURRENCIES
    }

    # Asset-class weight is currency-invariant (every class is converted by the same rate, so
    # the ratio cancels out) - only the absolute value differs, so the pie chart's % comes from
    # one currency while the companion table merges every currency's own value in, by class.
    allocation_by_class = {}
    for c in cfg.TRACKED_CURRENCIES:
        for row in all_time_reports[c]["end_allocation"]:
            entry = allocation_by_class.setdefault(row["class"], {"weight_pct": row["weight_pct"], "values": {}})
            entry["values"][c] = row["value"]
    allocation = [{"class": cls, **data} for cls, data in allocation_by_class.items()]
    allocation_totals = {
        "weight_pct": sum(row["weight_pct"] for row in allocation),
        "values": {c: sum(row["values"][c] for row in allocation) for c in cfg.TRACKED_CURRENCIES},
    }

    return templates.TemplateResponse(request, "overview.html", {
        "currencies": cfg.TRACKED_CURRENCIES,
        "currency": selected_currency,
        "periods": periods,
        "reports": reports,
        "net_worth": {c: all_time_reports[c]["end_value"] for c in cfg.TRACKED_CURRENCIES},
        "annualized": annualized,
        "as_of": all_time_reports[selected_currency]["end_date"],
        "allocation": allocation,
        "allocation_totals": allocation_totals,
    })


@app.get("/performance", response_class=HTMLResponse)
def view_performance(request: Request, scope: str = "portfolio", period: str = "all_time", currency: str = None,
                 start: str = None, end: str = None, benchmark: list[str] = Query(default=[]),
                 conn=Depends(get_db_connection)):
    _ensure_today_snapshot(conn)
    analytics = AnalyticsService(conn)
    scopes = analytics.list_scopes()
    account_id, asset_ids, scope_label = _resolve_scope(scope, scopes)
    chart_currency = currency if currency in cfg.TRACKED_CURRENCIES else cfg.TRACKED_CURRENCIES[0]

    windows = named_period_windows(datetime.date.today(), analytics.get_inception_date())
    periods = [(label, _period_display_name(label)) for label in windows.keys()] + [("custom", _period_display_name("custom"))]

    period_start, period_end = windows["all_time"]
    if period == "custom" and start and end:
        try:
            custom_start = datetime.date.fromisoformat(start)
            custom_end = min(datetime.date.fromisoformat(end), datetime.date.today())
            if custom_start <= custom_end:
                period_start, period_end = custom_start, custom_end
            else:
                period = "all_time"
        except ValueError:
            period = "all_time"
    else:
        period_start, period_end = windows.get(period, windows["all_time"])

    reports = {
        currency: analytics.get_period_report(period_start, period_end, currency,
                                               account_id=account_id, asset_ids=asset_ids)
        for currency in cfg.TRACKED_CURRENCIES
    }

    # Deposits/withdrawals split out of each report's already-computed cash_flows (XIRR sign
    # convention: deposit negative, withdrawal positive) - just a display-friendly reshaping.
    # "net" is cash movement only; "net_overall" is the actual value change (end - start),
    # which also captures market growth/loss on top of any deposits/withdrawals.
    cash_flow_summary = {}
    for c in cfg.TRACKED_CURRENCIES:
        deposits = sum(f["amount"] for f in reports[c]["cash_flows"] if f["amount"] < 0)
        withdrawals = sum(f["amount"] for f in reports[c]["cash_flows"] if f["amount"] > 0)
        cash_flow_summary[c] = { "start_value": -abs(reports[c]["start_value"]),
                                 "deposits": round(deposits, cfg.PRECISION),
                                 "withdrawals": round(withdrawals, cfg.PRECISION),
                                 "net": round(withdrawals + deposits, cfg.PRECISION),
                                 "end_value": reports[c]["end_value"],
                                 "net_overall": round(reports[c]["end_value"] - reports[c]["start_value"], cfg.PRECISION)}

    balances_end = analytics.get_asset_balances_by_account(period_end, cfg.TRACKED_CURRENCIES,
                                                            account_id=account_id, asset_ids=asset_ids)
    balances_start = analytics.get_asset_balances_by_account(period_start, cfg.TRACKED_CURRENCIES,
                                                              account_id=account_id, asset_ids=asset_ids)
    chart_start = period_start
    chart_data = _build_chart_data(reports[chart_currency], chart_start)

    evolution_svc = EvolutionService(conn)
    evolution = evolution_svc.get_evolution(chart_currency, chart_start, period_end, account_id, asset_ids)
    growth = evolution_svc.get_growth_comparison(chart_currency, chart_start, period_end, benchmark,
                                                  account_id, asset_ids)

    # Portfolio + selected-benchmarks metrics table (Entity | TWR per/ann | XIRR per/ann) - rows
    # scale with however many benchmarks are selected, never columns (see dashboard's benchmark
    # column-clutter fix). Annualizing is a cheap post-hoc conversion (annualize_rate) of an
    # already-solved rate, not a fresh solve - no per-day curve involved.
    days = (period_end - chart_start).days
    report = reports[chart_currency]
    metrics_rows = [{
        "label": "Portfolio",
        "twr_per": report["twr_pct"],
        "twr_ann": annualize_rate(report["twr_pct"] / 100, days) * 100 if report["twr_pct"] is not None else None,
        "xirr_per": report["xirr_pct"],
        "xirr_ann": annualize_rate(report["xirr_pct"] / 100, days) * 100,
    }]
    available_benchmarks = _available_benchmarks(scopes, chart_currency)
    benchmark_labels = {b["key"]: b["label"] for b in available_benchmarks}
    if benchmark:
        for key in benchmark:
            twr_series = growth["benchmarks"].get(key)
            if not twr_series:
                continue
            twr_per = twr_series[-1][1]
            metrics_rows.append({
                "label": benchmark_labels.get(key, key),
                "twr_per": twr_per,
                "twr_ann": annualize_rate(twr_per / 100, days) * 100,
                "xirr_per": None,
                "xirr_ann": None,
            })

    evolution_chart = {
        "value": _series_to_json([(p["date"], p["value"]) for p in evolution]),
        "net_invested": _series_to_json([(p["date"], p["net_invested"]) for p in evolution]),
    }
    total_gain = None
    total_gain_pct = None
    if evolution:
        last = evolution[-1]
        total_gain = round(last["value"] - last["net_invested"], cfg.PRECISION)
        if last["net_invested"] > 0:
            total_gain_pct = total_gain / last["net_invested"] * 100
    growth_chart = {
        "portfolio": _series_to_json(growth["portfolio"]),
        "benchmarks": {key: _series_to_json(series) for key, series in growth["benchmarks"].items()},
    }

    return templates.TemplateResponse(request, "performance.html", {
        "currencies": cfg.TRACKED_CURRENCIES,
        "currency_form_id": "performance-filters",
        "periods": periods,
        "reports": reports,
        "currency": chart_currency,
        "chart_data": chart_data,
        "cash_flow_summary": cash_flow_summary,
        "evolution_chart": evolution_chart,
        "total_gain": total_gain,
        "total_gain_pct": total_gain_pct,
        "growth_chart": growth_chart,
        "metrics_rows": metrics_rows,
        "available_benchmarks": available_benchmarks,
        "selected_benchmarks": benchmark,
        "scopes": scopes,
        "scope": scope,
        "scope_label": scope_label,
        "period": period,
        "period_start": period_start,
        "period_end": period_end,
        "chart_start": chart_start,
        "balances_start": balances_start,
        "balances_end": balances_end,
    })


@app.get("/exposure", response_class=HTMLResponse)
def view_exposure(request: Request, currency: str = None, conn=Depends(get_db_connection)):
    _ensure_today_snapshot(conn)
    chart_currency = currency or cfg.TRACKED_CURRENCIES[0]
    exposure = ExposureService(conn)

    return templates.TemplateResponse(request, "exposure.html", {
        "currencies": cfg.TRACKED_CURRENCIES,
        "currency": chart_currency,
        "holdings_tree": exposure.get_class_sector_holdings(chart_currency),
        "concentration": exposure.get_concentration(chart_currency),
        "currency_exposure": exposure.get_currency_exposure(chart_currency),
        "illiquid_exposure": exposure.get_illiquid_exposure(chart_currency),
    })


@app.get("/health")
def health():
    return {"status": "ok"}
