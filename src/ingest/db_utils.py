from psycopg2.extras import execute_values


def get_or_create_account(cur, name: str, account_type: str) -> int:
    cur.execute("""
        INSERT INTO accounts (name, account_type)
        VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE
        SET account_type = EXCLUDED.account_type
        RETURNING id;
    """, (name, account_type))
    return cur.fetchone()[0]


def get_or_create_asset(cur, symbol: str, asset_class: str, currency: str, is_liquid: bool = True) -> int:
    cur.execute("""
        INSERT INTO assets (symbol, asset_class, currency, is_liquid)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE
        SET currency = EXCLUDED.currency
        RETURNING id;
    """, (symbol, asset_class, currency, is_liquid))
    return cur.fetchone()[0]


def replace_account_transactions(cur, account_id: int, tx_records: list) -> int:
    """Idempotently replaces all transactions for an account (delete-then-insert). Caller commits."""
    cur.execute("DELETE FROM transactions WHERE account_id = %s;", (account_id,))
    if tx_records:
        execute_values(cur, """
            INSERT INTO transactions (
                account_id, asset_id, tx_type, executed_at, quantity, notes
            ) VALUES %s;
        """, tx_records)
    return len(tx_records)


def replace_account_asset_transactions(cur, account_id: int, asset_ids: list, tx_records: list) -> int:
    """Same as replace_account_transactions, scoped to a set of assets within the account"""
    cur.execute("DELETE FROM transactions WHERE account_id = %s AND asset_id = ANY(%s);", (account_id, asset_ids))
    if tx_records:
        execute_values(cur, """
            INSERT INTO transactions (
                account_id, asset_id, tx_type, executed_at, quantity, notes
            ) VALUES %s;
        """, tx_records)
    return len(tx_records)


def upsert_asset_prices(cur, price_records: list) -> None:
    """Bulk upsert of (asset_id, price_date, price) rows - price is always the nominal,
    as-actually-traded value (see asset_splits for corporate actions). Caller commits."""
    if not price_records:
        return
    execute_values(cur, """
        INSERT INTO asset_prices (asset_id, price_date, price)
        VALUES %s
        ON CONFLICT (asset_id, price_date) DO UPDATE
        SET price = EXCLUDED.price;
    """, price_records)


def upsert_asset_splits(cur, split_records: list) -> None:
    """Bulk upsert of (asset_id, split_date, ratio) rows - one per actual split event. Caller commits."""
    if not split_records:
        return
    execute_values(cur, """
        INSERT INTO asset_splits (asset_id, split_date, ratio)
        VALUES %s
        ON CONFLICT (asset_id, split_date) DO UPDATE
        SET ratio = EXCLUDED.ratio;
    """, split_records)


def get_price_at(cur, asset_id: int, date) -> float:
    """Latest known (nominal) price for `asset_id` on or before `date`, or None if nothing's
    been synced yet for it."""
    cur.execute("""
        SELECT price FROM asset_prices
        WHERE asset_id = %s AND price_date <= %s
        ORDER BY price_date DESC LIMIT 1;
    """, (asset_id, date))
    row = cur.fetchone()
    return float(row[0]) if row else None


