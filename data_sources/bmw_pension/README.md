# BMW Pension

Place the BMW pension "Kontoauszug" PDFs here, one PDF per fund.

1. Save each statement as `<fund>.pdf` (e.g. `pvk.pdf`, `ak.pdf`, `zv.pdf`).
2. List them in `config/config.yaml`:
   ```yaml
   data_sources:
     bmw_pension_pdfs:
       - "data_sources/bmw_pension/pvk.pdf"
       - "data_sources/bmw_pension/ak.pdf"
       - "data_sources/bmw_pension/zv.pdf"
   ```
3. Run `python src/portfolio_cron.py --once`.

Notes:
- The file name defines the asset: `pvk.pdf` becomes `BMW_PVK` (EUR, `long_term_savings`, illiquid), all under the account "BMW Pension".
- Parsed rows: `Einzahlung`, `Auszahlung`, `Fondsswitch/In`, `Fondsswitch/Out`. Unknown row types are logged and skipped.
- Every row also provides a unit price (NAV); the "Kursdatum" in the statement's holdings summary is used as the latest price.
- Re-running is safe (idempotent); replace a PDF with a newer statement to update.
- Statements are personal data - do not commit them.
