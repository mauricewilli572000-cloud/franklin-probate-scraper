"""Ohio probate lead pipeline (one county per run).

  python run_pipeline.py --county franklin    # nightly: last 10 days of new cases + retry pending
  python run_pipeline.py --days-back 120      # first run / backfill
  python run_pipeline.py --start 2026-10-01 --end 2026-10-31
  python run_pipeline.py --days-back 3 --limit 5 --dry-run   # quick test, writes nothing

Flow: court date index -> estate cases -> case detail (decedent address, date of death)
-> fiduciaries (executor mailing address/phone) + attorney -> docket flags
-> Auditor parcel match -> score -> data/records.json, GHL CSV, docs/records.json
"""
import argparse
import json
import logging
import shutil
import sys
import time
from datetime import date, datetime, timedelta

from probate import config, counties
from probate.export import export_ghl_csv
from probate.leads import bucket

log = logging.getLogger("probate")


# --------------------------------------------------------------------------- storage
def load(path):
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return {r["case_key"]: r for r in json.load(f)}
    return {}


def save(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(recs.values(), key=lambda r: (r.get("filing_date", ""), r["case_key"]), reverse=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
    tmp.replace(path)


def write_outputs(records, pending, skipped, counts, checkpoint=False, remaining=0):
    save(config.RECORDS_JSON, records)
    save(config.PENDING_JSON, pending)
    save(config.SKIPPED_JSON, skipped)
    n = export_ghl_csv(records.values(), config.GHL_CSV)
    config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config.RECORDS_JSON, config.DOCS_DIR / "records.json")
    shutil.copyfile(config.GHL_CSV, config.DOCS_DIR / config.GHL_CSV.name)
    with open(config.DOCS_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"county": config.DATA_DIR.name, "updated": datetime.now().isoformat(timespec="minutes"),
                   "leads": len(records),
                   "pending": len(pending), "last_run": counts, "remaining": remaining}, f)
    if not checkpoint:
        log.info("wrote %d leads, %d pending, %d skipped, %d CSV rows", len(records), len(pending), len(skipped), n)


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", default="franklin", help="one of: " + ", ".join(counties.COUNTIES))
    ap.add_argument("--days-back", type=int, default=config.DEFAULT_DAYS_BACK)
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--limit", type=int, default=0, help="only process N cases (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print leads, write nothing")
    ap.add_argument("--skip-auditor", action="store_true")
    ap.add_argument("--no-retry-pending", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=0,
                    help="stop cleanly after this long and save (next run continues)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    t0 = time.time()
    end = a.end or date.today()
    start = a.start or (end - timedelta(days=a.days_back))
    config.use_county(a.county)
    county = counties.load(a.county)()
    http = county.http

    records, pending, skipped = load(config.RECORDS_JSON), load(config.PENDING_JSON), load(config.SKIPPED_JSON)
    log.info("loaded %d records, %d pending, %d skipped", len(records), len(pending), len(skipped))

    log.info("county: %s", county.label)
    rows = county.scan(start, end)
    todo = {}
    for r in rows:
        k = r["case_number"] + r["suffix"]
        if k not in records and k not in skipped:
            todo[k] = r
    if not a.no_retry_pending:
        cutoff = (date.today() - timedelta(days=config.PENDING_MAX_DAYS)).isoformat()
        today = date.today().isoformat()
        for k, p in list(pending.items()):
            if k in todo or p.get("last_checked", "")[:10] == today:   # once a day is enough
                continue
            if p.get("filing_date", "") < cutoff:
                p["skip_reason"] = "no_property_found"
                skipped[k] = pending.pop(k)
                continue
            todo[k] = {"case_number": p["case_number"], "suffix": p["suffix"], "case_name": p["case_name"],
                       "case_type": "ESTATE", "subtype": p["subtype"], "status": p.get("court_status", ""),
                       "opened": p["filing_date"], "closed": p.get("closed_date", "")}
    items = sorted(todo.items(), key=lambda kv: kv[1].get("opened", ""), reverse=True)  # newest first
    if a.limit:
        items = items[: a.limit]
    log.info("%d estate cases to process (%d in index)", len(items), len(rows))

    counts = {"records": 0, "pending": 0, "skipped": 0, "errors": 0}
    stopped_early = False
    for i, (k, r) in enumerate(items, 1):
        if a.max_minutes and (time.time() - t0) / 60 > a.max_minutes:
            stopped_early = True
            log.warning("time limit reached after %d/%d cases - saving; the next run continues", i - 1, len(items))
            break
        if not a.dry_run and i % 25 == 0:
            write_outputs(records, pending, skipped, counts, checkpoint=True)
            el = (time.time() - t0) / 60
            log.info("checkpoint %d/%d saved (%.0f min elapsed, ~%.0f min left)", i, len(items), el,
                     el / i * (len(items) - i))
        try:
            lead = county.enrich(r, a.skip_auditor)
            lead["county"] = county.key
        except Exception as e:  # noqa: BLE001
            counts["errors"] += 1
            log.error("[%d/%d] %s failed: %s", i, len(items), k, e)
            continue
        b = bucket(lead)
        counts[b] += 1
        p = lead["property"]
        log.info("[%d/%d] %s %-30s %-6s %s %s", i, len(items), k, lead["case_name"][:30], b,
                 p.get("property_address", ""), f"score={lead['score']}" if p else lead.get("skip_reason", ""))
        if a.dry_run:
            continue
        for store in (records, pending, skipped):
            store.pop(k, None)
        if b == "records":
            old = records.get(k) or {}
            lead["date_added"] = old.get("date_added") or date.today().isoformat()
            records[k] = lead
        elif b == "pending":
            lead["first_seen"] = (pending.get(k) or {}).get("first_seen") or date.today().isoformat()
            pending[k] = lead
        else:
            skipped[k] = lead

    log.info("done: %s  (%d HTTP requests, %.1f min)%s", counts, http.requests_made,
             (time.time() - t0) / 60, "  [STOPPED EARLY - run again to continue]" if stopped_early else "")
    if a.dry_run:
        return 0
    write_outputs(records, pending, skipped, counts, remaining=len(items) - sum(counts.values()) if stopped_early else 0)
    return 0 if counts["errors"] < max(5, len(items) // 2) else 1


if __name__ == "__main__":
    sys.exit(main())
