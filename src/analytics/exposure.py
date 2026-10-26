import datetime
from config import cfg, ASSET_CLASS_LABELS
from analytics.utils import hub_to_currency_rate


class ExposureService:
    """Portfolio-wide exposure metrics (sector look-through, concentration, currency
    exposure, illiquid %) - always whole-portfolio, no account/asset scope."""

    def __init__(self, db_conn):
        self.conn = db_conn

    def _get_symbol_values(self, cur, snapshot_date: datetime.date) -> list:
        """[(symbol, asset_class, native_currency, is_liquid, value_hub), ...] as of
        `snapshot_date`, one row per symbol (summed across every account holding it)."""
        cur.execute("""
            SELECT a.symbol, a.asset_class, a.currency, a.is_liquid, SUM(s.market_value_base)
            FROM daily_portfolio_snapshots s
            JOIN assets a ON s.asset_id = a.id
            WHERE s.snapshot_date = %s
            GROUP BY a.symbol, a.asset_class, a.currency, a.is_liquid
            HAVING SUM(s.market_value_base) != 0;
        """, (snapshot_date,))
        return [(r[0], r[1], r[2], r[3], float(r[4])) for r in cur.fetchall()]

    @staticmethod
    def _lookthrough_stock_exposure(symbol_values: list) -> dict:
        """{resolved_symbol: value_hub} - splits each ETF's value across its constituent
        weights; the leftover % beyond the tracked top holdings goes into a shared
        "Unmapped" bucket. Everything else keeps its own symbol unchanged."""
        exposure = {}
        for symbol, _, _, _, value_hub in symbol_values:
            etf = cfg.ETF_HOLDINGS.get(symbol)
            if not etf:
                exposure[symbol] = exposure.get(symbol, 0.0) + value_hub
                continue
            covered_pct = 0.0
            for underlying, pct in etf["weights"].items():
                exposure[underlying] = exposure.get(underlying, 0.0) + value_hub * (pct / 100.0)
                covered_pct += pct
            leftover_pct = max(0.0, 100.0 - covered_pct)
            if leftover_pct:
                exposure["Unmapped"] = exposure.get("Unmapped", 0.0) + value_hub * (leftover_pct / 100.0)
        return exposure

    def get_sector_exposure(self, currency: str) -> list:
        """[{label, value, weight_pct}] sorted desc, as of today - equities resolved via
        `cfg.STOCK_SECTORS` (fallback "Unclassified"), non-equities bucketed by their own
        `asset_class` (Crypto/Fixed Income/Cash/Real Estate act as pseudo-sectors)."""
        snapshot_date = datetime.date.today()
        with self.conn.cursor() as cur:
            symbol_values = self._get_symbol_values(cur, snapshot_date)
            fx_rate = hub_to_currency_rate(cur, currency, snapshot_date)

        # A look-through-only constituent (e.g. AVGO via VOO/QQQ, never held directly) has no
        # row of its own in symbol_values - absent asset_class defaults to "equity" below since
        # every ETF weight entry is, by definition, a stock.
        asset_class_by_symbol = {symbol: asset_class for symbol, asset_class, _, _, _ in symbol_values}
        stock_exposure = self._lookthrough_stock_exposure(symbol_values)

        class_labels = ASSET_CLASS_LABELS
        sector_totals = {}
        for symbol, value_hub in stock_exposure.items():
            if symbol == "Unmapped":
                label = symbol
            else:
                asset_class = asset_class_by_symbol.get(symbol, "equity")
                label = (cfg.STOCK_SECTORS.get(symbol, "Unclassified") if asset_class == "equity"
                         else class_labels.get(asset_class, asset_class))
            sector_totals[label] = sector_totals.get(label, 0.0) + value_hub

        total_hub = sum(sector_totals.values())
        return sorted([
            {"label": label, "value": round(value * fx_rate, cfg.PRECISION),
             "weight_pct": round(value / total_hub * 100, cfg.PRECISION) if total_hub else 0.0}
            for label, value in sector_totals.items()
        ], key=lambda r: r["value"], reverse=True)

    def get_concentration(self, currency: str, top_n: int = 10) -> dict:
        """{"top_holdings": [{label, value, weight_pct}] (top `top_n`), "hhi": float} - HHI is
        the Herfindahl-Hirschman Index (0-1 scale: 1.0 = a single holding, 1/N = N
        equal-weighted), computed over every resolved holding, not just the displayed `top_n`."""
        snapshot_date = datetime.date.today()
        with self.conn.cursor() as cur:
            symbol_values = self._get_symbol_values(cur, snapshot_date)
            fx_rate = hub_to_currency_rate(cur, currency, snapshot_date)

        stock_exposure = self._lookthrough_stock_exposure(symbol_values)
        total_hub = sum(stock_exposure.values())
        if not total_hub:
            return {"top_holdings": [], "hhi": 0.0}

        rows = sorted([
            {"label": symbol, "value": round(value * fx_rate, cfg.PRECISION),
             "weight_pct": round(value / total_hub * 100, cfg.PRECISION)}
            for symbol, value in stock_exposure.items()
        ], key=lambda r: r["value"], reverse=True)
        hhi = sum((value / total_hub) ** 2 for value in stock_exposure.values())

        return {"top_holdings": rows[:top_n], "hhi": round(hhi, 4)}

    def get_currency_exposure(self, currency: str) -> list:
        """[{label, value, weight_pct}] sorted desc, as of today - grouped by each holding's
        own native `assets.currency` (not the display currency), converted into `currency`."""
        snapshot_date = datetime.date.today()
        with self.conn.cursor() as cur:
            symbol_values = self._get_symbol_values(cur, snapshot_date)
            fx_rate = hub_to_currency_rate(cur, currency, snapshot_date)

        totals = {}
        for _, _, native_currency, _, value_hub in symbol_values:
            totals[native_currency] = totals.get(native_currency, 0.0) + value_hub
        total_hub = sum(totals.values())

        return sorted([
            {"label": native_currency, "value": round(value * fx_rate, cfg.PRECISION),
             "weight_pct": round(value / total_hub * 100, cfg.PRECISION) if total_hub else 0.0}
            for native_currency, value in totals.items()
        ], key=lambda r: r["value"], reverse=True)

    def get_illiquid_exposure(self, currency: str) -> dict:
        """{"liquid_value", "illiquid_value", "illiquid_pct"} as of today, in `currency` -
        grouped by `assets.is_liquid`."""
        snapshot_date = datetime.date.today()
        with self.conn.cursor() as cur:
            symbol_values = self._get_symbol_values(cur, snapshot_date)
            fx_rate = hub_to_currency_rate(cur, currency, snapshot_date)

        liquid_hub = sum(v for _, _, _, is_liquid, v in symbol_values if is_liquid)
        illiquid_hub = sum(v for _, _, _, is_liquid, v in symbol_values if not is_liquid)
        total_hub = liquid_hub + illiquid_hub

        return {
            "liquid_value": round(liquid_hub * fx_rate, cfg.PRECISION),
            "illiquid_value": round(illiquid_hub * fx_rate, cfg.PRECISION),
            "illiquid_pct": round(illiquid_hub / total_hub * 100, cfg.PRECISION) if total_hub else 0.0,
        }

    def get_class_sector_holdings(self, currency: str, top_n: int = 4) -> dict:
        """Class -> Sector -> Holding look-through hierarchy: {"classes": [...], "total":
        {...}}. Only the top `top_n` holdings per sector are kept, remainder rolled into
        "Others". `weight_pct` at every level is always % of the grand total."""
        snapshot_date = datetime.date.today()
        with self.conn.cursor() as cur:
            symbol_values = self._get_symbol_values(cur, snapshot_date)
            fx_rate = hub_to_currency_rate(cur, currency, snapshot_date)

        asset_class_by_symbol = {symbol: asset_class for symbol, asset_class, _, _, _ in symbol_values}
        stock_exposure = self._lookthrough_stock_exposure(symbol_values)

        class_labels = ASSET_CLASS_LABELS
        tree = {}  # class_label -> sector_label -> symbol -> value_hub
        for symbol, value_hub in stock_exposure.items():
            if symbol == "Unmapped":
                # Untracked ETF weight is still equity look-through (e.g. VOO/QQQ), so it
                # belongs under the Equity class, just in its own "Unmapped" sector.
                class_label = class_labels["equity"]
                sector_label = "Unmapped"
            else:
                asset_class = asset_class_by_symbol.get(symbol, "equity")
                class_label = class_labels.get(asset_class, asset_class)
                sector_label = (cfg.STOCK_SECTORS.get(symbol, "Unclassified") if asset_class == "equity"
                                 else class_label)
            sectors = tree.setdefault(class_label, {})
            holdings = sectors.setdefault(sector_label, {})
            holdings[symbol] = holdings.get(symbol, 0.0) + value_hub

        total_hub = sum(v for sectors in tree.values() for holdings in sectors.values()
                         for v in holdings.values())

        def weight_pct(value_hub):
            return round(value_hub / total_hub * 100, cfg.PRECISION) if total_hub else 0.0

        classes = []
        for class_label, sectors in tree.items():
            class_total = sum(v for holdings in sectors.values() for v in holdings.values())
            sector_rows = []
            for sector_label, holdings in sectors.items():
                sector_total = sum(holdings.values())
                sorted_holdings = sorted(holdings.items(), key=lambda kv: kv[1], reverse=True)
                holding_rows = [
                    {"label": label, "value": round(v * fx_rate, cfg.PRECISION), "weight_pct": weight_pct(v)}
                    for label, v in sorted_holdings[:top_n]
                ]
                others_hub = sum(v for _, v in sorted_holdings[top_n:])
                if others_hub > 0:
                    holding_rows.append({"label": "Others", "value": round(others_hub * fx_rate, cfg.PRECISION),
                                          "weight_pct": weight_pct(others_hub)})
                sector_rows.append({"label": sector_label, "value": round(sector_total * fx_rate, cfg.PRECISION),
                                     "weight_pct": weight_pct(sector_total), "holdings": holding_rows})
            sector_rows.sort(key=lambda r: r["value"], reverse=True)
            classes.append({"label": class_label, "value": round(class_total * fx_rate, cfg.PRECISION),
                             "weight_pct": weight_pct(class_total), "sectors": sector_rows})
        classes.sort(key=lambda r: r["value"], reverse=True)

        return {
            "classes": classes,
            "total": {"value": round(total_hub * fx_rate, cfg.PRECISION), "weight_pct": 100.0 if total_hub else 0.0},
        }
