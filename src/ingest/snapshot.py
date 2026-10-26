import datetime
from analytics.utils import HUB_CURRENCY

class SnapshotService:
    def __init__(self, db_conn):
        self.conn = db_conn

    def generate_daily_snapshots(self, start_date: datetime.date = None, end_date: datetime.date = None):
        """Generates one valuation snapshot per calendar day from `start_date` (default:
        inception) through `end_date` (default: today). Always recomputes the full range, so a
        retroactively-added historical source is reflected on every date, not just from ingestion."""
        end_date = end_date or datetime.date.today()
        if start_date is None:
            with self.conn.cursor() as cur:
                cur.execute("SELECT MIN(executed_at)::date FROM transactions;")
                row = cur.fetchone()
            start_date = row[0] if row and row[0] else end_date

        count = 0
        d = start_date
        while d <= end_date:
            self.generate_snapshot(d)
            count += 1
            d += datetime.timedelta(days=1)
        print(f"Generated {count} daily snapshots from {start_date} to {end_date}.")

    def generate_snapshot(self, target_date: datetime.date):
        """Generates the valuation snapshot as of `target_date`: holdings, prices, and FX
        rates are all bounded to what was known on or before that date. `market_value_base`
        is always denominated in HUB_CURRENCY."""
        with self.conn.cursor() as cur:
            # 1. Clean this date's snapshot before rebuilding
            cur.execute("DELETE FROM daily_portfolio_snapshots WHERE snapshot_date = %s;", (target_date,))

            # 2. Compute holdings, native valuations, and converted base valuations as of target_date
            cur.execute("""
                INSERT INTO daily_portfolio_snapshots (
                    snapshot_date, account_id, asset_id,
                    quantity_held, market_value_base
                )
                WITH latest_prices AS (
                    SELECT DISTINCT ON (asset_id) asset_id, price
                    FROM asset_prices
                    WHERE price_date <= %s
                    ORDER BY asset_id, price_date DESC
                ),
                latest_fx AS (
                    SELECT DISTINCT ON (ca.symbol) ca.symbol AS currency, ap.price AS rate
                    FROM asset_prices ap
                    JOIN assets ca ON ca.id = ap.asset_id AND ca.asset_class = 'cash'
                    WHERE ap.price_date <= %s
                    ORDER BY ca.symbol, ap.price_date DESC
                ),
                current_holdings AS (
                    SELECT
                        t.account_id,
                        t.asset_id,
                        SUM(t.quantity * COALESCE(sr.factor, 1.0)) AS qty
                    FROM transactions t
                    LEFT JOIN LATERAL (
                        -- Cumulative product of every split recorded after this transaction's date
                        -- and on/before this snapshot's own target_date - a split that, as of
                        -- target_date, hasn't happened yet must not forward-adjust quantity (the
                        -- price paired with it here is asset_prices' nominal, as-actually-traded
                        -- value for that day, see yahoo.py, not retroactively split-adjusted).
                        SELECT EXP(SUM(LN(s.ratio))) AS factor
                        FROM asset_splits s
                        WHERE s.asset_id = t.asset_id
                          AND s.split_date > t.executed_at::date
                          AND s.split_date <= %s
                    ) sr ON true
                    WHERE t.executed_at::date <= %s
                    GROUP BY t.account_id, t.asset_id
                    HAVING ABS(SUM(t.quantity * COALESCE(sr.factor, 1.0))) > 0.000001
                )
                SELECT
                    %s AS snapshot_date,
                    h.account_id,
                    h.asset_id,
                    h.qty AS quantity_held,
                    (h.qty * (CASE WHEN a.asset_class = 'cash' THEN 1.0 ELSE COALESCE(lp.price, 0.0) END)) * COALESCE(lfx.rate, 1.0) AS market_value_base
                FROM current_holdings h
                JOIN assets a ON h.asset_id = a.id
                LEFT JOIN latest_prices lp ON h.asset_id = lp.asset_id
                LEFT JOIN latest_fx lfx ON a.currency = lfx.currency;
            """, (target_date, target_date, target_date, target_date, target_date))

        self.conn.commit()
        print(f"Generated daily valuation snapshots for {target_date} in {HUB_CURRENCY} (hub).")
