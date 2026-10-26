from datetime import datetime
from pathlib import Path
import pdfplumber
from ingest.db_utils import get_or_create_account, get_or_create_asset, replace_account_asset_transactions, upsert_asset_prices

ACCOUNT_NAME = "BMW Pension"

# Row "Ein-/Auszahlung" values that actually appear in these statements - only used to validate
# a row is real (log an error and skip it otherwise). BUY vs SELL comes from the sign of the
# already-extracted quantity, not from this text, so it never needs to be branched on.
KNOWN_TYPES = {"Einzahlung", "Auszahlung", "Fondsswitch/In", "Fondsswitch/Out"}


class BmwPensionProvider:
    """Parses every BMW pension 'Kontoauszug' PDF into transactions, all filed under one shared
    'BMW Pension' account - one asset per PDF/fund. Fund switches (Fondsswitch/In and
    Fondsswitch/Out) collapse into that asset's running quantity, no fund-level performance."""

    def __init__(self, db_conn, pdf_paths: list):
        self.conn = db_conn
        self.pdf_paths = pdf_paths

    @staticmethod
    def _parse_de_number(token: str) -> float:
        """German number format: '.' thousands separator, ',' decimal separator."""
        return float(token.strip().replace(".", "").replace(",", "."))

    @staticmethod
    def _is_month_year(token: str) -> bool:
        """'mm/yyyy', e.g. '08/2023' - the Belegdatum column of a real ledger row. Stricter than
        just checking for a '/', which also matches the header row ('mm/jjjj') and summary-table
        fund names like 'BMW AK/ZV Target Fonds'."""
        return len(token) == 7 and token[2] == "/" and token[:2].isdigit() and token[3:].isdigit()

    @staticmethod
    def _is_day_month_year(token: str) -> bool:
        """'dd.mm.yyyy', e.g. '31.07.2026' - the Kursdatum column of a holdings-summary row."""
        parts = token.split(".")
        return len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4

    def _extract_rows(self, pdf_path: str):
        """Returns (transaction rows, extra (date, price) points from the holdings-summary
        table). That table's Kursdatum is the statement's own as-of/cutoff date, often more
        recent than any ledger transaction, so it's a genuinely newer price observation."""
        rows = []
        extra_prices = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    for row in table:
                        if not row or not row[0]:
                            continue
                        first_cell = str(row[0]).strip()

                        if self._is_month_year(first_cell):
                            row_type = (row[1] or "").strip()
                            if row_type not in KNOWN_TYPES:
                                print(f"ERROR: unrecognized BMW pension row type '{row_type}' in "
                                      f"'{pdf_path}': {row!r}")
                                continue
                            rows.append({
                                "date": datetime.strptime(row[4].strip(), "%d.%m.%Y").date(),
                                "quantity": self._parse_de_number(row[5]),
                                "unit_price": self._parse_de_number(row[6]),
                                "amount": self._parse_de_number(row[7]),
                                "notes": row_type,
                            })
                        elif len(row) == 5 and row[3] and self._is_day_month_year(str(row[3]).strip()):
                            extra_prices.append((
                                datetime.strptime(row[3].strip(), "%d.%m.%Y").date(),
                                self._parse_de_number(row[2]),
                            ))
        return rows, extra_prices

    def sync(self):
        with self.conn.cursor() as cur:
            account_id = get_or_create_account(cur, ACCOUNT_NAME, "bank")
            eur_asset_id = get_or_create_asset(cur, "EUR", "cash", "EUR", is_liquid=True)

            tx_records = []
            price_records = []
            touched_asset_ids = [eur_asset_id]
            total_rows = 0

            for pdf_path in self.pdf_paths:
                symbol = f"BMW_{Path(pdf_path).stem.upper()}"
                asset_id = get_or_create_asset(cur, symbol, "long_term_savings", "EUR", is_liquid=False)
                touched_asset_ids.append(asset_id)

                rows, extra_prices = self._extract_rows(pdf_path)
                if not rows:
                    raise ValueError(f"No transaction rows found in '{pdf_path}'.")

                price_points = {}  # date -> price - every row is also a NAV observation
                for row in rows:
                    tx_type = "BUY" if row["quantity"] >= 0 else "SELL"
                    boundary_type = "DEPOSIT" if tx_type == "BUY" else "WITHDRAWAL"
                    internal_type = "SELL" if tx_type == "BUY" else "BUY"
                    amount = abs(row["amount"])

                    tx_records.append((
                        account_id, asset_id, tx_type, row["date"], row["quantity"],
                        f"{row['notes']} @ EUR {row['unit_price']:,.2f} (EUR {amount:,.2f})"
                    ))
                    # Real DEPOSIT/WITHDRAWAL of EUR (external capital marker) immediately
                    # spent/received via a SELL/BUY of that same EUR (internal, same date) - so
                    # EUR's own quantity nets back to zero instead of accumulating a phantom
                    # balance across every pension fund sharing this account.
                    boundary_qty = amount if boundary_type == "DEPOSIT" else -amount
                    tx_records.append((
                        account_id, eur_asset_id, boundary_type, row["date"], boundary_qty,
                        f"Auto {boundary_type} for: {row['notes']} ({symbol})"
                    ))
                    tx_records.append((
                        account_id, eur_asset_id, internal_type, row["date"], -boundary_qty,
                        f"Auto {internal_type} (funding leg) for: {row['notes']} ({symbol})"
                    ))
                    price_points[row["date"]] = row["unit_price"]

                # The holdings-summary table's Kursdatum is often newer than any ledger row (the
                # statement's own as-of/cutoff date) - let it win on an overlapping date.
                price_points.update(extra_prices)
                price_records.extend((asset_id, date, price) for date, price in price_points.items())
                total_rows += len(rows)

            replace_account_asset_transactions(cur, account_id, touched_asset_ids, tx_records)
            upsert_asset_prices(cur, price_records)

        self.conn.commit()
        print(f"Successfully synced {total_rows} statement rows across {len(self.pdf_paths)} BMW pension "
              f"PDF(s) (idempotent).")
