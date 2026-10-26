from datetime import datetime
import pandas as pd
from ingest.db_utils import (get_or_create_account, get_or_create_asset, get_price_at,
                             replace_account_transactions, upsert_asset_prices)

ACCOUNT_NAME = "Tesouro Direto"

# "Movimentação" values that actually appear in the B3 export, mapped to our tx_type. Anything
# else (Venda, Resgate, Juros Semestrais, ...) is logged as an error and skipped rather than
# guessed at - none of those have been observed in a real export yet.
MOVEMENT_TX_TYPES = {
    "Compra": "BUY",
    "Cobrança de Taxa Semestral": "FEE",
}


class TesouroDiretoProvider:
    """Ingests a B3 'Extrato de Movimentação' xlsx export (Tesouro Direto) into transactions,
    all filed under one 'Tesouro Direto' account, one asset per distinct title ('Produto'). No
    internal cash sub-account is tracked, so a BUY auto-generates a DEPOSIT sibling."""

    def __init__(self, db_conn, xlsx_path: str):
        self.conn = db_conn
        self.xlsx_path = xlsx_path

    @staticmethod
    def _symbol_for(produto: str) -> str:
        return produto.upper().replace("+", "").replace(" ", "_")

    def sync(self):
        df = pd.read_excel(self.xlsx_path, sheet_name=0)

        with self.conn.cursor() as cur:
            account_id = get_or_create_account(cur, ACCOUNT_NAME, "fixed_income")

            asset_ids = {}
            tx_records = []
            price_points = {}  # (asset_id, date) -> price, from each purchase's own unit price
            for _, row in df.iterrows():
                movement = str(row["Movimentação"]).strip()
                tx_type = MOVEMENT_TX_TYPES.get(movement)
                if tx_type is None:
                    raise ValueError(f"ERROR: unrecognized Tesouro Direto movement '{movement}' in "
                          f"'{self.xlsx_path}': {row.to_dict()!r}")

                produto = str(row["Produto"]).strip()
                symbol = self._symbol_for(produto)
                if symbol not in asset_ids:
                    asset_ids[symbol] = get_or_create_asset(cur, symbol, "fixed_income", "BRL", is_liquid=True)
                asset_id = asset_ids[symbol]

                executed_at = datetime.strptime(str(row["Data"]).strip(), "%d/%m/%Y")
                quantity = float(row["Quantidade"])
                raw_price = row["Preço unitário"]
                unit_price = float(raw_price) if isinstance(raw_price, (int, float)) else 0.0
                amount = abs(float(row["Valor da Operação"]))
                notes = f"{movement} - {produto} @ BRL {unit_price:,.2f} (BRL {amount:,.2f})"

                tx_records.append((
                    account_id, asset_id, tx_type, executed_at, quantity, notes
                ))

                if tx_type == "BUY":
                    # Prefer the title's own canonical price (asset_prices, same series
                    # used for valuation everywhere else) over the B3 export's own executed price,
                    # so the funding leg agrees with the title's own valuation - falls back to the
                    # export's amount if no price is synced yet (e.g. a from-scratch DB's very
                    # first ingestion pass).
                    price = get_price_at(cur, asset_id, executed_at.date())
                    funding_amount = quantity * price if price is not None else amount
                    # Real DEPOSIT of BRL (external capital marker) immediately spent via a
                    # SELL of that same BRL (internal, same date) - so BRL's own quantity nets
                    # back to zero instead of accumulating a phantom balance across every title.
                    brl_asset_id = get_or_create_asset(cur, "BRL", "cash", "BRL", is_liquid=True)
                    tx_records.append((
                        account_id, brl_asset_id, "DEPOSIT", executed_at, funding_amount,
                        f"Auto DEPOSIT for: {movement} - {produto}"
                    ))
                    tx_records.append((
                        account_id, brl_asset_id, "SELL", executed_at, -funding_amount,
                        f"Auto SELL (funding leg) for: {movement} - {produto}"
                    ))
                    price_points[(asset_id, executed_at.date())] = unit_price
                elif tx_type == "FEE":
                    # No asset is acquired in return, so there's no third leg like BUY's: fund
                    # it (DEPOSIT) then immediately pay it out (FEE), both external - the
                    # fee never inflates invested capital, and BRL's own balance still nets back
                    # to zero instead of drifting negative forever.
                    brl_asset_id = get_or_create_asset(cur, "BRL", "cash", "BRL", is_liquid=True)
                    tx_records.append((
                        account_id, brl_asset_id, "DEPOSIT", executed_at, amount,
                        f"Auto DEPOSIT (funding) for: {movement} - {produto}"
                    ))
                    tx_records.append((
                        account_id, brl_asset_id, "FEE", executed_at, -amount,
                        f"Auto FEE (fee payment) for: {movement} - {produto}"
                    ))

            replace_account_transactions(cur, account_id, tx_records)
            price_records = [(asset_id, date, price) for (asset_id, date), price in price_points.items()]
            upsert_asset_prices(cur, price_records)

        self.conn.commit()
        print(f"Successfully synced {len(tx_records)} transactions and {len(price_records)} price points "
              f"for account '{ACCOUNT_NAME}' from '{self.xlsx_path}' (idempotent).")
