import csv
from datetime import datetime
from ingest.db_utils import get_or_create_account, get_or_create_asset, get_price_at, replace_account_transactions

SATS_PER_BTC = 100_000_000


class SparrowProvider:
    """Parses a Sparrow Wallet transaction export (CSV) for one watch-only cold storage wallet.
    Each row becomes 3 legs: the BTC itself, plus a DEPOSIT/WITHDRAWAL of USD immediately
    offset by a SELL/BUY of that same USD, so USD's own quantity nets back to zero."""

    def __init__(self, db_conn, csv_path: str, account_name: str = "Sparrow Wallet"):
        self.conn = db_conn
        self.csv_path = csv_path
        self.account_name = account_name

    def sync(self):
        with open(self.csv_path, "r", encoding="utf-8-sig") as f:
            rows = [
                row for row in csv.DictReader(f)
                if row.get("Date (UTC)") and not row["Date (UTC)"].strip().startswith("#")
            ]

        with self.conn.cursor() as cur:
            account_id = get_or_create_account(cur, self.account_name, "crypto_wallet")
            asset_id = get_or_create_asset(cur, "BTC", "crypto", "USD", is_liquid=True)
            usd_asset_id = get_or_create_asset(cur, "USD", "cash", "USD", is_liquid=True)

            tx_records = []
            for row in rows:
                date_str = row["Date (UTC)"].strip()
                executed_at = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")

                qty = float(row["Value"].strip()) / SATS_PER_BTC
                # Prefer BTC's own canonical price (asset_prices, same series used for
                # valuation everywhere else) over Sparrow's own quote, so the funding leg agrees
                # with the asset's own valuation - falls back to Sparrow's quote if no price is
                # synced yet for that date (e.g. a from-scratch DB's very first ingestion pass).
                price = get_price_at(cur, asset_id, executed_at.date())
                usd_value = abs(qty) * price if price is not None else abs(float(row["Value (USD)"].strip()))
                notes = f"{row['Label'].strip()} (txid {row['Txid'].strip()[:10]}..., ~${usd_value:,.2f})"

                tx_records.append((
                    account_id, asset_id, "BUY" if qty > 0 else "SELL", executed_at, qty, notes
                ))
                # No pre-existing USD balance to fund this from (a cold wallet has no internal
                # cash sub-account): model as a real DEPOSIT/WITHDRAWAL of USD (external capital
                # marker) immediately spent/received via a SELL/BUY of that same USD (internal,
                # same date) - so USD's own quantity nets back to zero instead of accumulating a
                # phantom balance.
                if qty > 0:
                    tx_records.append((account_id, usd_asset_id, "DEPOSIT", executed_at, usd_value, notes))
                    tx_records.append((account_id, usd_asset_id, "SELL", executed_at, -usd_value, notes))
                else:
                    tx_records.append((account_id, usd_asset_id, "BUY", executed_at, usd_value, notes))
                    tx_records.append((account_id, usd_asset_id, "WITHDRAWAL", executed_at, -usd_value, notes))

            replace_account_transactions(cur, account_id, tx_records)

        self.conn.commit()
        print(f"Successfully synced {len(tx_records)} transactions for {self.account_name}.")
