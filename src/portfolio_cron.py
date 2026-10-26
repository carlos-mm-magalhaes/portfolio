import os
import sys
import time
import schedule
import psycopg2
from config import cfg
from ingest.transactions.yaml import YamlProvider
from ingest.transactions.schwab import SchwabProvider
from ingest.transactions.nexo import NexoProvider
from ingest.transactions.sparrow import SparrowProvider
from ingest.transactions.bmw_pension import BmwPensionProvider
from ingest.transactions.tesouro_direto import TesouroDiretoProvider
from ingest.market_data.yahoo import YahooMarketData
from ingest.market_data.tesouro_direto import TesouroDiretoMarketData
from ingest.snapshot import SnapshotService


def _safe(label: str, fn):
    """Runs one ingestion/sync step in isolation - a single transaction or market-data source
    failing (network hiccup, a source file changing shape, etc.) must not abort the whole
    nightly run, especially snapshot generation at the very end."""
    try:
        fn()
    except Exception as e:
        print(f"[ERROR] {label} failed: {e}")


def _sync_file(label: str, provider_cls, conn, path):
    """Runs a file-based provider only if its source file is configured and present."""
    if not path or not os.path.isfile(path):
        print(f"[SKIP] {label}: source file not configured or missing ({path})")
        return
    _safe(label, provider_cls(conn, path).sync)


def run_update():
    """One full ingestion pass: transactions, market data/FX, then daily snapshots."""
    conn = psycopg2.connect(**cfg.get_db_connection_params())

    print("=== Ingesting Transaction Data ===")
    for yaml_path in cfg.YAML_SOURCES:
        _sync_file(f"YamlProvider({yaml_path})", YamlProvider, conn, yaml_path)
    _sync_file("SchwabProvider", SchwabProvider, conn, cfg.SCHWAB_CSV_PATH)
    _sync_file("NexoProvider", NexoProvider, conn, cfg.NEXO_CSV_PATH)
    _sync_file("SparrowProvider", SparrowProvider, conn, cfg.SPARROW_CSV_PATH)
    pension_pdfs = [p for p in cfg.BMW_PENSION_PDF_PATHS if os.path.isfile(p)]
    if pension_pdfs:
        _safe("BmwPensionProvider", BmwPensionProvider(conn, pension_pdfs).sync)
    else:
        print("[SKIP] BmwPensionProvider: no PDFs found")
    _sync_file("TesouroDiretoProvider", TesouroDiretoProvider, conn, cfg.TESOURO_DIRETO_XLSX_PATH)

    print("=== Ingesting Asset Infos (e.g. Prices, Splits, FX) ===")
    yahoo_svc = YahooMarketData(conn)
    tesouro_svc = TesouroDiretoMarketData(conn)
    _safe("sync_fx_rates", yahoo_svc.sync_fx_rates)
    _safe("sync_equity_prices", yahoo_svc.sync_equity_prices)
    _safe("sync_crypto_prices", yahoo_svc.sync_crypto_prices)
    _safe("sync_benchmark_prices", yahoo_svc.sync_benchmark_prices)
    _safe("sync_tesouro_direto_prices", tesouro_svc.sync_prices)

    # Re-sync the two providers whose funding legs derive from a canonical price (see
    # get_price_at) now that this pass's market data is available - both are idempotent
    # (delete-then-reinsert), so this just recomputes those legs from the freshest price
    # instead of relying on next run's data (matters most on a from-scratch DB's first run).
    _sync_file("SparrowProvider (re-sync)", SparrowProvider, conn, cfg.SPARROW_CSV_PATH)
    _sync_file("TesouroDiretoProvider (re-sync)", TesouroDiretoProvider, conn, cfg.TESOURO_DIRETO_XLSX_PATH)

    print("=== Generating Snapshots ===")
    _safe("generate_daily_snapshots", SnapshotService(conn).generate_daily_snapshots)

    conn.close()


def run_update_safe():
    """Same as run_update(), but never lets a bad run crash the persistent scheduler loop."""
    print("[CRON] Starting daily portfolio database sync...", flush=True)
    try:
        run_update()
        print("[CRON] Database sync completed successfully.", flush=True)
    except Exception as e:
        print(f"[CRON ERROR] Database sync failed: {e}", flush=True)


if __name__ == "__main__":
    if "--once" in sys.argv:
        # Manual/interactive run (README) - let real exceptions surface with a full traceback.
        run_update()
    else:
        if os.getenv("RUN_ON_STARTUP", "false").lower() == "true":
            run_update_safe()

        schedule.every().day.at("02:00").do(run_update_safe)
        print("[CRON] Scheduler initialized. Next run scheduled for 02:00 daily.", flush=True)
        while True:
            schedule.run_pending()
            time.sleep(30)
