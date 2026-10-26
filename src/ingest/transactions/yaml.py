import yaml
from datetime import datetime
import psycopg2
from config import cfg
from ingest.db_utils import (
    get_or_create_account,
    get_or_create_asset,
    replace_account_transactions,
    upsert_asset_prices,
)


class YamlProvider:
    """Generic ingestion for one account and its one-or-more YAML-declared assets (real estate,
    fixed income, physical holdings, ...), recording transactions and appraisal valuations."""

    def __init__(self, db_conn, yaml_path: str):
        self.conn = db_conn
        self.yaml_path = yaml_path

    def sync(self):
        with open(self.yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        account_info = data.get("account", {})
        account_name = account_info.get("name", "Physical Assets")
        account_type = account_info.get("account_type", "physical")

        assets_data = data.get("assets", [])
        if not assets_data:
            raise ValueError(f"Invalid YAML '{self.yaml_path}': 'assets' list is required.")

        with self.conn.cursor() as cur:
            account_id = get_or_create_account(cur, account_name, account_type)

            tx_records = []
            val_records = []
            symbols = []

            for asset_info in assets_data:
                symbol = asset_info.get("symbol", "").strip().upper()
                if not symbol:
                    raise ValueError(f"Invalid YAML '{self.yaml_path}': 'asset.symbol' is required.")
                symbols.append(symbol)

                asset_class = asset_info.get("asset_class", "real_estate")
                currency = asset_info.get("currency", "USD").upper()
                is_liquid = bool(asset_info.get("is_liquid", False))
                asset_id = get_or_create_asset(cur, symbol, asset_class, currency, is_liquid)

                # 1. Ingest Transactions
                implied_valuations = {}  # date -> price, seeded from the acquisition cost basis
                for tx in asset_info.get("transactions", []):
                    tx_date = datetime.strptime(str(tx["date"]), "%Y-%m-%d").date()
                    tx_type = tx.get("type", "BUY").upper()
                    amount = float(tx.get("amount", 0.0))
                    tx_curr = tx.get("currency", currency).upper()
                    notes = tx.get("notes", f"{tx_type} for {symbol}")

                    # DEPOSIT/WITHDRAWAL cash flow does not change asset units (qty = 0)
                    if tx_type in ["DEPOSIT", "WITHDRAWAL"]:
                        qty = 0.0
                    elif tx_type == "BUY":
                        qty = float(tx.get("quantity", 1.0))
                        # A real buy/sell establishes the asset's price as of that date, so
                        # snapshots before the first appraisal value it at cost instead of zero.
                        implied_valuations.setdefault(tx_date, abs(amount/qty))
                    elif tx_type == "SELL":
                        qty = -abs(float(tx.get("quantity", 1.0)))
                        implied_valuations.setdefault(tx_date, abs(amount/qty))
                    else:  # FEE or others
                        qty = 0.0

                    tx_records.append((
                        account_id, asset_id, tx_type, tx_date, qty,
                        f"{notes} (${amount:,.2f} {tx_curr})"
                    ))

                    # Manually tracked assets have no internal cash sub-account: by default, the
                    # money for a BUY/SELL crosses the portfolio boundary (bank account, salary,
                    # ...) rather than coming from/going to another tracked holding. Set
                    # `external: false` on a transaction to opt out (e.g. funded by selling
                    # another tracked asset instead). Modeled as a real DEPOSIT/WITHDRAWAL of
                    # that currency (external capital marker) immediately spent/received via a
                    # SELL/BUY of that same currency (internal leg, same date) - so the currency
                    # asset's own quantity nets back to zero instead of accumulating a phantom
                    # balance, while still deriving its dollar amount from asset_prices
                    # like everything else (no amount column needed).
                    if tx_type in ("BUY", "SELL") and amount > 0 and bool(tx.get("external", True)):
                        boundary_type = "DEPOSIT" if tx_type == "BUY" else "WITHDRAWAL"
                        internal_type = "SELL" if tx_type == "BUY" else "BUY"
                        cash_asset_id = get_or_create_asset(cur, tx_curr, "cash", tx_curr, is_liquid=True)
                        boundary_qty = amount if boundary_type == "DEPOSIT" else -amount
                        tx_records.append((
                            account_id, cash_asset_id, boundary_type, tx_date, boundary_qty,
                            f"Auto {boundary_type} for: {notes}"
                        ))
                        tx_records.append((
                            account_id, cash_asset_id, internal_type, tx_date, -boundary_qty,
                            f"Auto {internal_type} (funding leg) for: {notes}"
                        ))
                    elif tx_type == "FEE" and amount > 0 and bool(tx.get("external", True)):
                        # No asset is acquired in return, so there's no third leg like BUY's:
                        # fund it (DEPOSIT) then immediately pay it out (FEE), both
                        # external - the fee never inflates invested capital, and the currency's
                        # own balance still nets back to zero instead of drifting negative.
                        cash_asset_id = get_or_create_asset(cur, tx_curr, "cash", tx_curr, is_liquid=True)
                        tx_records.append((
                            account_id, cash_asset_id, "DEPOSIT", tx_date, amount,
                            f"Auto DEPOSIT (funding) for: {notes}"
                        ))
                        tx_records.append((
                            account_id, cash_asset_id, "FEE", tx_date, -amount,
                            f"Auto FEE (fee payment) for: {notes}"
                        ))

                # 2. Ingest Historical Valuations / Appraisals (explicit appraisals win over the
                # acquisition-cost fallback on any overlapping date)
                for val in asset_info.get("valuations", []):
                    val_date = datetime.strptime(str(val["date"]), "%Y-%m-%d").date()
                    implied_valuations[val_date] = float(val.get("price", 0.0))

                val_records.extend((asset_id, val_date, price) for val_date, price in implied_valuations.items())

            replace_account_transactions(cur, account_id, tx_records)
            upsert_asset_prices(cur, val_records)

        self.conn.commit()
        print(f"Successfully synced {len(tx_records)} transactions and {len(val_records)} valuations for {', '.join(symbols)} (idempotent).")
