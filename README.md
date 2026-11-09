# Franklin County, OH — Probate Lead Scraper

Nightly scraper for new **estate** cases at the Franklin County Probate Court, matched to
Franklin County Auditor parcels so you only get estates that actually own real estate.
Built like the Franklin / Cuyahoga foreclosure scrapers: JSON accumulation, pending retry
file, GHL CSV, GitHub Actions nightly run, GitHub Pages dashboard.

## How it works

| Step | Source | What we get |
|---|---|---|
| 1. Date index | `probatesearch.franklincountyohio.gov/netdata/PBODateInx.ndm/input?string=YYYYMMDD` (paged 40 at a time via `stringf=`) | every case opened that day; keep Type = ESTATE |
| 2. Estate detail | `PBCaseTypeE.ndm/ESTATE_DETAIL?caseno=NNNNNN;;` | decedent street/city/zip, **date of death**, subtype |
| 3. Fiduciaries | `PBFidy.ndm/input` → `PBFidDetail.ndm/FID_DETAIL` | executor/applicant **name, mailing address, phone** (= your contact) |
| 4. Attorney | `PBAttyDetail.ndm/ATTY_DETAIL` | estate attorney name, address, phone |
| 5. Docket | `PBDocket.ndm/input` | flags: Auditor's Real Estate Summary filed, motion to sell real estate, etc. |
| 6. Auditor | `gis.franklincountyohio.gov/.../Parcel_Features/MapServer/0/query` (live, 495k parcels) | parcel, owner, value, last sale, mortgage escrow payer, year built |

Parcel status per lead:
- **in_decedent_name** – house still titled to the decedent (best lead)
- **transferred_to_heirs** – deeded after death for ~$0 (certificate of transfer); heirs own it
- **sold** / **other_owner** – ruled out → `data/skipped_records.json`

Matches by address are high-confidence. Matches by name alone (other parcels the decedent
owned) need the middle name/initial to agree and are labelled `confidence: medium` unless
the middle name matches fully or the owner's mailing address is the decedent's home.

Estates with no parcel match (apartments, Delaware/Licking County addresses, Auditor slow to
update) go to `data/pending_records.json` and are re-checked every run for 60 days.

## Files

```
run_pipeline.py          entry point
probate/config.py        URLs + settings
probate/client.py        HTTP with retries / polite delay
probate/parsers.py       court page parsers
probate/auditor.py       address/name normalisation + parcel matching
probate/scoring.py       0-100 lead score (reasons stored with each lead)
probate/export.py        GHL CSV (contact = executor)
docs/index.html          dashboard (reads docs/records.json)
data/records.json        leads  |  pending_records.json  |  skipped_records.json
data/ghl_probate_leads.csv
tests/                   parser + pipeline tests on real saved court pages
```

## Run locally (PowerShell)

```powershell
cd $HOME\Documents\franklin-probate-scraper
pip install -r requirements.txt
python -m pytest -q
python run_pipeline.py --days-back 3 --limit 5 --dry-run   # quick look, writes nothing
python run_pipeline.py --days-back 120                     # first backfill (~15 estates/day, ~6 requests each)
```

## GitHub setup (own repo, like the eviction / excess-funds repos)

1. Create a new empty repo `franklin-probate-scraper` on GitHub.
2. In PowerShell:
   ```powershell
   cd $HOME\Documents\franklin-probate-scraper
   git init; git add .; git commit -m "Franklin probate scraper"
   git branch -M main
   git remote add origin https://github.com/<you>/franklin-probate-scraper.git
   git push -u origin main
   ```
3. Repo → Settings → Actions → General → Workflow permissions → **Read and write**.
4. Repo → Settings → Pages → Deploy from branch → `main` / `/docs`.
5. Actions → *Franklin probate leads* → **Run workflow** with `days_back = 120` for the backfill.
   After that it runs every morning (~6:37 AM ET) scanning the last 10 days.

Dashboard: `https://<you>.github.io/franklin-probate-scraper/`
JSON for the combined dashboard's **Probate** tab: `https://<you>.github.io/franklin-probate-scraper/records.json`

## Lead fields (records.json)

`case_key, case_number, lead_type="Probate", case_name, decedent_first/last, subtype, filing_date,
date_of_death, decedent_street/city/zip, fiduciary{name,title,street,city,state,zip,phone},
attorney{name,phone,...}, property{parcel_id, property_address, property_city, property_zip,
auditor_owner, market_value, last_sale_date, last_sale_price, has_mortgage_escrow, tax_bill_payer,
property_class, year_built, beds, baths, rental, status, matched_by, confidence},
other_properties[], docket_flags{}, score, score_reasons[], court_url, date_added`
