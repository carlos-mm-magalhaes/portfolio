# Data Sources

Drop the raw exports/statements of each broker, bank or institution here, then point
`config/config.yaml` (`data_sources:` section) at them. Paths are relative to the repo root.

## Supported formats

| Source | Format | Config key | Template |
| --- | --- | --- | --- |
| Manual assets (real estate, physical, fixed income, ...) | YAML | `yaml_sources` (list) | `template_manual.yaml` |
| Charles Schwab | CSV export | `schwab_csv` | `template_schwab.csv` |
| Nexo | Transactions CSV | `nexo_csv` | - |
| Sparrow (crypto wallet) | CSV export | `sparrow_csv` | - |
| Tesouro Direto (B3 "Movimentacao") | XLSX export | `tesouro_direto_xlsx` | - |
| BMW Pension | "Kontoauszug" PDFs | `bmw_pension_pdfs` (list) | see `bmw_pension/README.md` |

## Usage

1. Copy the matching template (or export from the provider) into this folder.
2. Reference the file in `config/config.yaml`, e.g.:
   ```yaml
   data_sources:
     yaml_sources:
       - "data_sources/my_assets.yaml"
     schwab_csv: "data_sources/schwab.csv"
   ```
3. Run the ingestion: `python src/portfolio_cron.py --once`.

Each provider replaces its own transactions on every run (delete-then-insert), so the files are
the source of truth: edit/replace them and re-run, no manual DB cleanup needed. Rows a provider
can't classify are logged as errors and skipped.

## Manual YAML

One file = one account with one or more assets (see `template_manual.yaml`):

- `account`: `name`, `account_type`.
- `assets[]`: `symbol`, `asset_class`, `currency`, `is_liquid`.
- `transactions[]`: `date` (`YYYY-MM-DD`), `type` (`BUY`, `SELL`, `DEPOSIT`, `WITHDRAWAL`, `FEE`),
  `amount`, `quantity`, `currency`, `notes`. A `BUY`/`SELL`/`FEE` is by default funded from outside
  the portfolio; set `external: false` if it was funded by another tracked asset.
- `valuations[]`: dated appraisals (`date`, `price`) used as the asset's price history.

> Do not commit personal exports; only the templates belong in version control.
