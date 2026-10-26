import datetime
import io
import os
import tempfile
import time
import unicodedata
import pandas as pd
import requests
from config import cfg
from ingest.db_utils import upsert_asset_prices

# Tesouro Transparente's CKAN dataset - one government-wide historical price/rate CSV covering
# every Tesouro Direto title ever offered, cached locally rather than re-fetched on every call.
TESOURO_CACHE_FILE = os.path.join(tempfile.gettempdir(), "tesouro_direto_prices.csv")
TESOURO_CKAN_URL = "https://www.tesourotransparente.gov.br/ckan/api/3/action/package_show"
TESOURO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/csv, */*",
}


class TesouroDiretoMarketData:
    """Fetches Tesouro Direto title prices from Tesouro Transparente's public CKAN dataset -
    one government-wide CSV covering every title ever offered, cached locally (12h TTL)."""

    def __init__(self, db_conn):
        self.conn = db_conn

    @staticmethod
    def _normalize(text) -> str:
        """Strips accents/case for a reliable string match against Tesouro's own title names
        (e.g. 'Tesouro IPCA+' vs 'Tesouro IPCA+ com Juros Semestrais' must not collide)."""
        return unicodedata.normalize("NFKD", str(text)).encode("ASCII", "ignore").decode("utf-8").strip().lower()

    def _get_csv_url(self) -> str:
        res = requests.get(
            TESOURO_CKAN_URL,
            params={"id": "taxas-dos-titulos-ofertados-pelo-tesouro-direto"},
            headers=TESOURO_HEADERS,
            timeout=15,
        )
        res.raise_for_status()
        for resource in res.json().get("result", {}).get("resources", []):
            name = self._normalize(resource.get("name", "") + " " + resource.get("url", ""))
            if "precotaxa" in name or "preco_taxa" in name:
                return resource["url"]
        raise ValueError("Could not find the Tesouro Direto price/rate dataset in the CKAN catalog.")

    def _fetch_price_history(self, cache_ttl_hours: int = 12) -> pd.DataFrame:
        """Downloads (or loads from cache) the full historical price dataset - large and
        government-wide, not specific to this portfolio."""
        if os.path.exists(TESOURO_CACHE_FILE):
            age_hours = (time.time() - os.path.getmtime(TESOURO_CACHE_FILE)) / 3600
            if age_hours < cache_ttl_hours:
                return pd.read_csv(TESOURO_CACHE_FILE, parse_dates=["data_base", "data_vencimento"])

        url = self._get_csv_url()
        resp = requests.get(url, headers=TESOURO_HEADERS, timeout=45)
        resp.raise_for_status()

        separator = ";" if ";" in resp.text.split("\n", 1)[0] else ","
        df = pd.read_csv(io.StringIO(resp.text), sep=separator, decimal=",", encoding="utf-8-sig", low_memory=False)
        df.columns = [self._normalize(c).replace(" ", "_").replace("-", "_") for c in df.columns]
        df["data_base"] = pd.to_datetime(df["data_base"], format="%d/%m/%Y", errors="coerce")
        df["data_vencimento"] = pd.to_datetime(df["data_vencimento"], format="%d/%m/%Y", errors="coerce")

        df.to_csv(TESOURO_CACHE_FILE, index=False)
        return df

    def _price_series(self, tipo: str, vencimento: str, start_date: datetime.date) -> list:
        """[(date, price), ...] for one title since `start_date`, using the sell-back price
        ("PU Venda") - its mark-to-market value, not the original purchase price."""
        df = self._fetch_price_history()
        target = self._normalize(tipo)
        venc_dt = pd.to_datetime(vencimento, dayfirst=True)
        price_col = "pu_venda_manha" if "pu_venda_manha" in df.columns else "pu_resgate_manha"

        conditions = ((df["tipo_titulo"].apply(self._normalize) == target)
                      & (df["data_vencimento"] == venc_dt)
                      & (df["data_base"] >= pd.to_datetime(start_date)))
        matches = df[conditions].sort_values("data_base")
        return [(row["data_base"].date(), float(row[price_col])) for _, row in matches.iterrows()
                if pd.notna(row[price_col])]

    def sync_prices(self):
        """Daily price history for every Tesouro Direto title in cfg.TESOURO_DIRETO_TITLES
        (symbol -> {tipo, vencimento}), since portfolio inception - resolved via that mapping
        since the maturity year alone can't key the CKAN dataset's history."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT MIN(executed_at)::date FROM transactions;")
            row = cur.fetchone()
            start_date = row[0] if row and row[0] else datetime.date.today()

            total = 0
            for symbol, title in cfg.TESOURO_DIRETO_TITLES.items():
                cur.execute("SELECT id FROM assets WHERE symbol = %s;", (symbol,))
                asset_row = cur.fetchone()
                if not asset_row:
                    continue
                try:
                    series = self._price_series(title["tipo"], title["vencimento"], start_date)
                except Exception as e:
                    print(f"Could not fetch Tesouro Direto price history for {symbol}: {e}")
                    continue
                if series:
                    upsert_asset_prices(cur, [(asset_row[0], d, p) for d, p in series])
                    total += len(series)
            self.conn.commit()
        print(f"Synced {total} historical Tesouro Direto prices.")
