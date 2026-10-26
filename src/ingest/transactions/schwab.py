import csv
import re
from datetime import datetime
import psycopg2
from config import cfg
from ingest.db_utils import (
    get_or_create_account,
    get_or_create_asset,
    replace_account_transactions,
)


class SchwabProvider:
    """Ingestion for csv exported file from Schwab account."""

    def __init__(self, db_conn, csv_path: str, account_name: str = "Charles Schwab"):
        self.conn = db_conn
        self.csv_path = csv_path
        self.account_name = account_name

    @staticmethod
    def _clean_numeric(value: str) -> float:
        """Strips currency symbols, commas, and whitespace to return float."""
        if not value or value.strip() == "":
            return 0.0
        cleaned = re.sub(r"[^\d.-]", "", value.strip())
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """Parses Schwab MM/DD/YYYY or MM/DD/YYYY as of MM/DD/YYYY dates."""
        clean_date = date_str.split(" as of ")[0].strip()
        return datetime.strptime(clean_date, "%m/%d/%Y")

    def sync(self):
        with open(self.csv_path, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        # Find the line where the actual CSV table headers start
        header_idx = None
        for i, line in enumerate(lines):
            if "Date" in line and "Action" in line and "Symbol" in line:
                header_idx = i
                break

        if header_idx is None:
            raise ValueError("Invalid Schwab CSV: Column headers not found.")

        reader = csv.DictReader(lines[header_idx:])
        rows = list(reader)

        with self.conn.cursor() as cur:
            account_id = get_or_create_account(cur, self.account_name, "brokerage")
            usd_cash_id = get_or_create_asset(cur, "USD", "cash", "USD")

            # 1. Ingest Transactions
            tx_records = []

            for row in rows:
                raw_date = row.get("Date", "").strip()
                action = row.get("Action", "").strip()
                symbol = row.get("Symbol", "").strip().upper()
                symbol = cfg.SYMBOL_ALIASES.get(symbol, symbol)
                desc = row.get("Description", "").strip()

                if not raw_date or not action:
                    raise ValueError(f"Invalid Schwab CSV: Missing required data in row: {row}")

                # Ignore merger transfer entries between TDA and Schwab
                if "OUTGOING ACCOUNT TRANSFER" in desc or "TDA TO CS&CO TRANSFER" in desc:
                    print(f"Skipping merger transfer entry: {desc} on {raw_date}")
                    continue

                executed_at = self._parse_date(raw_date)
                quantity = self._clean_numeric(row.get("Quantity", ""))
                price = self._clean_numeric(row.get("Price", ""))
                raw_amount = self._clean_numeric(row.get("Amount", ""))
                abs_amount = abs(raw_amount)

                # 1. TRADES: BUY / REINVEST
                if action in ["Buy"]:
                    asset_id = get_or_create_asset(cur, symbol, "equity", "USD")
                    # Asset leg: +Shares
                    tx_records.append((
                        account_id, asset_id, "BUY", executed_at,
                        abs(quantity), f"{action} - {desc} @ ${price} (${abs_amount:,.2f})"
                    ))
                    # Cash counter-leg: -USD Cash
                    tx_records.append((
                        account_id, usd_cash_id, "BUY", executed_at,
                        -abs_amount, f"Cash settlement for {action} {symbol}"
                    ))

                # 2. TRADES: SELL
                elif action in ["Sell"]:
                    asset_id = get_or_create_asset(cur, symbol, "equity", "USD")
                    # Asset leg: -Shares
                    tx_records.append((
                        account_id, asset_id, "SELL", executed_at,
                        -abs(quantity), f"{action} - {desc} @ ${price} (${abs_amount:,.2f})"
                    ))
                    # Cash counter-leg: +USD Cash
                    tx_records.append((
                        account_id, usd_cash_id, "SELL", executed_at,
                        abs_amount, f"Cash proceeds from {action} {symbol}"
                    ))

                # 3. DIVIDENDS
                elif action in ["Cash Dividend", "Qual Dividend", "Qualified Dividend", "Qual Div Reinvest", "Div Adjust"]:
                    tx_records.append((
                        account_id, usd_cash_id, "DIVIDEND", executed_at,
                        abs_amount, f"{action} - {desc}"
                    ))

                # 4. INTEREST
                elif action in ["Bank Interest", "Credit Interest", "Bond Interest"]:
                    tx_records.append((
                        account_id, usd_cash_id, "INTEREST", executed_at,
                        abs_amount, f"{action} - {desc}"
                    ))

                # 5. DEPOSITS & WITHDRAWALS
                elif (action in ["MoneyLink Deposit", "Internal Transfer"]) or (action in ["MoneyLink Transfer"] or ("CASH MOVEMENT OF OUTGOING ACCOUNT TRANSFER" in desc)):
                    if raw_amount >= 0:
                        tx_type = "DEPOSIT"
                        qty = abs_amount
                    else:
                        tx_type = "WITHDRAWAL"
                        qty = -abs_amount

                    tx_records.append((
                        account_id, usd_cash_id, tx_type, executed_at,
                        qty, f"{action} - {desc}"
                    ))

                # 6. TAXES & FEES
                elif "Tax" in action or "Fee" in action or action in ["NRA Tax Adj", "Journaled Shares"] or "WITHHOLDING" in desc:
                    tx_records.append((
                        account_id, usd_cash_id, "FEE", executed_at,
                        -abs_amount, f"{action} - {desc}"
                    ))
                else:
                    raise ValueError(f"Unknown action encountered in Schwab CSV: {action} ({desc}) on {raw_date}")

            replace_account_transactions(cur, account_id, tx_records)

        self.conn.commit()
        print(f"Successfully processed and inserted {len(tx_records)} ledger entries.")
