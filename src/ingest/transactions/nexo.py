import csv
import re
from collections import defaultdict
from datetime import datetime
from ingest.db_utils import get_or_create_account, get_or_create_asset, replace_account_transactions

# Nexo-native stablecoins are wrapped fiat, pegged 1:1 - fold them into the real currency
STABLECOIN_ALIASES = {"EURX": "EUR", "USDX": "USD", "EUR": "EUR", "USD": "USD"}
CRYPTO_ASSETS = {"BTC", "ETH", "DOGE", "SOL", "NEXO"}

# Same-currency in/out reward payouts (in-kind yield, no cash flow impact)
INTEREST_TYPES = {"Interest", "Fixed Term Interest", "Dividend", "Exchange Cashback"}
# No external effect, safe to skip entirely: "Exchange Deposited On" is a duplicate ledger leg
# of "Deposit To Exchange" (same amount/timestamp); term-deposit locks/unlocks always move the
# same currency/amount in and out, netting to zero (verified across the full Nexo export)
SKIPPED_TYPES = {"Exchange Deposited On", "Locking Term Deposit", "Unlocking Term Deposit"}
# Snap a reduction to a full drain when it's this close to the tracked balance, absorbing the
# per-trade rounding dust Nexo's own CSV accumulates once a currency is fully exited
FULL_DRAIN_TOLERANCE = 0.001


class NexoProvider:
    def __init__(self, db_conn, csv_path: str, account_name: str = "Nexo"):
        self.conn = db_conn
        self.csv_path = csv_path
        self.account_name = account_name

    @staticmethod
    def _parse_amount(value: str) -> float:
        value = value.strip()
        return 0.0 if not value or value == "-" else float(value)

    @staticmethod
    def _parse_usd_equivalent(value: str) -> float:
        cleaned = re.sub(r"[^\d.-]", "", value.strip())
        return float(cleaned) if cleaned not in ("", "-", ".") else 0.0

    @staticmethod
    def _native_currency(symbol: str) -> str:
        if symbol in STABLECOIN_ALIASES:
            return STABLECOIN_ALIASES[symbol]
        elif symbol in CRYPTO_ASSETS:
            return "USD"
        else:
            raise ValueError(f"Unrecognized asset symbol: {symbol}")

    def _asset_id(self, cur, symbol: str) -> int:
        asset_class = "crypto" if symbol in CRYPTO_ASSETS else "cash"
        return get_or_create_asset(cur, symbol, asset_class, self._native_currency(symbol), is_liquid=True)

    @staticmethod
    def _reduce_balance(balances: dict, symbol: str, requested: float) -> float:
        """Reduces balances[symbol] by `requested`, snapping to a full drain when `requested`
        is within FULL_DRAIN_TOLERANCE of what's available, so fully exiting a currency
        (withdrawal or final exchange) never leaves permanent rounding dust behind."""
        available = max(balances[symbol], 0.0)
        if available and abs(requested - available) <= available * FULL_DRAIN_TOLERANCE:
            qty = available
        else:
            qty = min(requested, available)
        balances[symbol] -= qty
        return qty

    def sync(self):
        with open(self.csv_path, "r", encoding="utf-8-sig") as f:
            # Chronological order is required so the withdrawal balance-clamp below (which relies
            # on what has already accrued) sees deposits/interest/exchanges before withdrawals.
            rows = sorted(
                csv.DictReader(f),
                key=lambda r: datetime.strptime(r["Date / Time (UTC)"].strip(), "%Y-%m-%d %H:%M:%S"),
            )

            with self.conn.cursor() as cur:
                account_id = get_or_create_account(cur, self.account_name, "crypto_wallet")

                tx_records = []
                balances = defaultdict(float)  # running native-unit balance per currency

                for row in rows:
                    tx_type = row["Type"].strip()
                    if tx_type in SKIPPED_TYPES:
                        continue

                    executed_at = datetime.strptime(row["Date / Time (UTC)"].strip(), "%Y-%m-%d %H:%M:%S")
                    in_curr = row["Input Currency"].strip().upper()
                    in_curr = STABLECOIN_ALIASES.get(in_curr, in_curr)
                    out_curr = row["Output Currency"].strip().upper()
                    out_curr = STABLECOIN_ALIASES.get(out_curr, out_curr)
                    in_amount = self._parse_amount(row["Input Amount"])
                    out_amount = self._parse_amount(row["Output Amount"])
                    usd_value = self._parse_usd_equivalent(row["USD Equivalent"])
                    notes = f"{tx_type} - {row['Details'].strip()}"

                    # Reward payouts: same currency in/out, in-kind yield
                    if tx_type in INTEREST_TYPES:
                        asset_id = self._asset_id(cur, in_curr)
                        qty = abs(in_amount)
                        tx_records.append((
                            account_id, asset_id, "INTEREST", executed_at,
                            qty, f"{notes} (~${usd_value:,.2f})"
                        ))
                        balances[in_curr] += qty

                    # Real bank top-up landing on the platform as a stablecoin
                    elif tx_type == "Deposit To Exchange":
                        asset_id = self._asset_id(cur, out_curr)
                        qty = abs(out_amount)
                        tx_records.append((
                            account_id, asset_id, "DEPOSIT", executed_at,
                            qty, notes
                        ))
                        balances[out_curr] += qty

                    # Real bank withdrawal leaving the platform
                    elif tx_type in ("Withdraw Exchanged", "Withdrawal"):
                        withdrawn_curr = out_curr if tx_type == "Withdraw Exchanged" else in_curr
                        asset_id = self._asset_id(cur, withdrawn_curr)
                        qty = self._reduce_balance(balances, withdrawn_curr, abs(in_amount))
                        tx_records.append((
                            account_id, asset_id, "WITHDRAWAL", executed_at,
                            -qty, notes
                        ))

                    # Exchange / Exchange To Withdraw: internal swap between two tracked assets,
                    # zero external cash flow
                    else:
                        sell_id = self._asset_id(cur, in_curr)
                        buy_id = self._asset_id(cur, out_curr)
                        sell_qty = self._reduce_balance(balances, in_curr, abs(in_amount))
                        buy_qty = abs(out_amount)
                        tx_records.append((
                            account_id, sell_id, "SELL", executed_at,
                            -sell_qty, f"{notes} (sell leg, ~${usd_value:,.2f})"
                        ))
                        tx_records.append((
                            account_id, buy_id, "BUY", executed_at,
                            buy_qty, f"{notes} (buy leg, ~${usd_value:,.2f})"
                        ))
                        balances[out_curr] += buy_qty

                replace_account_transactions(cur, account_id, tx_records)

            self.conn.commit()
            print(f"Successfully processed and inserted {len(tx_records)} ledger entries from Nexo.")
