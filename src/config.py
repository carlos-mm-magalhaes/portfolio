import os
import datetime
import yaml
from pathlib import Path
from dotenv import load_dotenv

# Load .env secrets from repo root
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

# Shared display labels for `assets.asset_class` values - used anywhere a class is shown to
# the user (allocation pie/table, sector exposure fallback), so every chart stays consistent.
ASSET_CLASS_LABELS = {
    "crypto": "Crypto", "fixed_income": "Fixed Income", "cash": "Cash",
    "real_estate": "Real Estate", "equity": "Equity",
    "long_term_savings": "Long-Term Savings", "other": "Other",
}

class Config:
    def __init__(self, config_path: str = "config/config.yaml"):
        full_path = BASE_DIR / config_path
        with open(full_path, "r", encoding="utf-8") as f:
            self._raw = yaml.safe_load(f)
        # Reference data that outgrows a single config file (top-N ETF holdings, sector map) -
        # kept alongside config.yaml, own lifecycle (periodically re-pasted from a factsheet).
        # One file per ETF (etf_holdings_<symbol>.yaml) since each is updated independently.
        self._etf_holdings = {}
        for symbol in ("voo", "qqq"):
            with open(BASE_DIR / f"config/etf_holdings_{symbol}.yaml", "r", encoding="utf-8") as f:
                self._etf_holdings[symbol.upper()] = yaml.safe_load(f)
        # stock_sectors.yaml groups symbols under each sector for readability; flatten to a
        # symbol -> sector dict, the lookup shape the rest of the app expects.
        with open(BASE_DIR / "config/stock_sectors.yaml", "r", encoding="utf-8") as f:
            self._stock_sectors = {
                symbol: group["sector"]
                for group in yaml.safe_load(f)
                for symbol in group["symbols"]
            }

        # Database credentials (from environment or defaults)
        self.DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
        self.DB_PORT = os.getenv("DB_PORT", "5432")
        self.POSTGRES_DB = os.getenv("POSTGRES_DB", "portfolio_db")
        self.POSTGRES_USER = os.getenv("POSTGRES_USER", "admin")
        self.POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "admin_password")

        # Portfolio domain settings
        self.TRACKED_CURRENCIES = self._raw["portfolio"].get("tracked_currencies", ["USD", "EUR", "BRL", "CHF"])
        # Floor date for price/FX backfill and the named-period year list - independent of
        # any single transaction's date, so results don't depend on which broker/account
        # happened to record the earliest trade.
        self.HISTORY_START_DATE = datetime.date.fromisoformat(str(self._raw["portfolio"]["history_start_date"]))

        # File paths
        self.YAML_SOURCES = [str(BASE_DIR / p) for p in self._raw["data_sources"].get("yaml_sources", [])]
        # Every source is optional; a key left out of config.yaml (None) is skipped by the cron.
        sources = self._raw.get("data_sources", {})
        def _path(key):
            return str(BASE_DIR / sources[key]) if sources.get(key) else None
        self.SCHWAB_CSV_PATH = _path("schwab_csv")
        self.NEXO_CSV_PATH = _path("nexo_csv")
        self.SPARROW_CSV_PATH = _path("sparrow_csv")
        self.BMW_PENSION_PDF_PATHS = [str(BASE_DIR / p) for p in sources.get("bmw_pension_pdfs", [])]
        self.TESOURO_DIRETO_XLSX_PATH = _path("tesouro_direto_xlsx")

        # Mappings & Analytics
        self.SYMBOL_ALIASES = self._raw.get("symbol_aliases", {})
        self.TESOURO_DIRETO_TITLES = self._raw.get("tesouro_direto", {}).get("titles", {})
        self.BENCHMARKS = self._raw.get("benchmarks", {})
        self.ETF_HOLDINGS = self._etf_holdings
        self.STOCK_SECTORS = self._stock_sectors
        self.XIRR_DEFAULT_GUESS = self._raw["analytics"].get("xirr_default_guess", 0.10)
        self.PRECISION = self._raw["analytics"].get("precision_decimals", 2)

    def get_db_connection_params(self) -> dict:
        return {
            "dbname": self.POSTGRES_DB,
            "user": self.POSTGRES_USER,
            "password": self.POSTGRES_PASSWORD,
            "host": self.DB_HOST,
            "port": self.DB_PORT,
        }

# Singleton instance for imports
cfg = Config()
