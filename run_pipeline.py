"""Franklin County, OH probate lead pipeline.

  python run_pipeline.py                      # nightly: last 10 days of new cases + retry pending
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
from datetime import date, datetime, timedelta

from probate import config
from probate.auditor import Auditor, name_parts
from probate.client import Http, ProbateSite
from probate.export import export_ghl_csv
from probate import parsers
from probate.scoring import score

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


# --------------------------------------------------------------------------- scanning
def scan_index(site, start, end):
    """All cases opened between start and end (inclusive), following 'Next Cases' paging."""
    rows, seen_keys = [], set()
    html = site.date_index(yyyymmdd=start.strftime("%Y%m%d"))
    pages = 0
    while True:
        pages += 1
        page_rows, next_key, _ = parsers.parse_date_index(html)
        stop = False
        for r in page_rows:
            if not r["opened"]:
                continue
            if r["opened"] > end.isoformat():
                stop = True
                break
            if r["opened"] >= start.isoformat():
                rows.append(r)
        if stop or not page_rows or not next_key or next_key in seen_keys or pages > 400:
            break
        seen_keys.add(next_key)
        html = site.date_index(next_key=next_key)
    log.info("date index: %d cases opened %s..%s (%d pages)", len(rows), start, end, pages)
    return rows


# --------------------------------------------------------------------------- enrichment
def enrich(site, auditor, row, skip_auditor=False):
    cn, sfx = row["case_number"], row["suffix"]
    det = parsers.parse_estate_detail(site.estate_detail(cn, sfx))

    fids = parsers.parse_fiduciary_list(site.fiduciaries(cn, sfx))
    fid_details, attorney = [], {}
    active = [f for f in fids if not f["terminated"]] or fids
    for f in active[: config.MAX_FIDUCIARIES]:
        try:
            d = parsers.parse_fiduciary_detail(site.fiduciary_detail(cn, f["fid_no"], sfx))
            d["fid_no"] = f["fid_no"]
            fid_details.append(d)
        except Exception as e:  # noqa: BLE001
            log.warning("%s fiduciary %s: %s", cn, f["fid_no"], e)
        if not attorney and f["attorney_number"]:
            try:
                attorney = parsers.parse_attorney_detail(site.attorney_detail(cn, f["fid_no"], sfx))
            except Exception as e:  # noqa: BLE001
                log.warning("%s attorney: %s", cn, e)

    try:
        flags = parsers.docket_flags(parsers.parse_docket(site.docket(cn, sfx)))
    except Exception as e:  # noqa: BLE001
        log.warning("%s docket: %s", cn, e)
        flags = {}

    props, err = ([], "skipped") if skip_auditor else auditor.match(
        det["case_name"] or row["case_name"], det["decedent_street"], det["decedent_zip"],
        det["date_of_death"])

    last, first, middle = name_parts(row["case_name"])
    key = cn + sfx
    lead = {
        "case_key": key,
        "case_number": cn,
        "suffix": sfx,
        "lead_type": "Probate",
        "case_name": row["case_name"],
        "decedent_last": last.title(),
        "decedent_first": first.title(),
        "decedent_middle": middle.title(),
        "aka": det["aka"],
        "subtype": det["subtype"] or row["subtype"],
        "court_status": row["status"],
        "filing_date": row["opened"],
        "closed_date": row["closed"] or det["date_closed"],
        "date_of_death": det["date_of_death"],
        "decedent_street": det["decedent_street"],
        "decedent_city": det["decedent_city"],
        "decedent_state": det["decedent_state"],
        "decedent_zip": det["decedent_zip"],
        "fiduciary": fid_details[0] if fid_details else ({"name": fids[0]["name"], "title": fids[0]["title"]} if fids else {}),
        "fiduciaries": fid_details,
        "attorney": attorney,
        "docket_flags": flags,
        "court_url": f"{config.ESTATE_DETAIL_URL}?caseno={cn};{sfx};",
        "auditor_error": err,
        "last_checked": datetime.now().isoformat(timespec="seconds"),
    }
    good = [p for p in props if p["status"] in ("in_decedent_name", "transferred_to_heirs")]
    lead["property"] = good[0] if good else {}
    lead["other_properties"] = good[1:]
    lead["ruled_out_properties"] = [p for p in props if p not in good]
    lead["score"], lead["score_reasons"] = score(lead)
    return lead


def bucket(lead):
    """records / pending / skipped"""
    if lead["property"]:
        return "records"
    if lead.get("auditor_error"):
        return "pending"
    if lead["ruled_out_properties"]:
        sold = any(p["status"] == "sold" for p in lead["ruled_out_properties"])
        lead["skip_reason"] = "sold_after_death" if sold else "decedent_not_owner"
        return "skipped"
    return "pending"


# --------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days-back", type=int, default=config.DEFAULT_DAYS_BACK)
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--limit", type=int, default=0, help="only process N cases (testing)")
    ap.add_argument("--dry-run", action="store_true", help="print leads, write nothing")
    ap.add_argument("--skip-auditor", action="store_true")
    ap.add_argument("--no-retry-pending", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    end = a.end or date.today()
    start = a.start or (end - timedelta(days=a.days_back))
    http = Http()
    site, auditor = ProbateSite(http), Auditor(http)

    records, pending, skipped = load(config.RECORDS_JSON), load(config.PENDING_JSON), load(config.SKIPPED_JSON)
    log.info("loaded %d records, %d pending, %d skipped", len(records), len(pending), len(skipped))

    rows = [r for r in scan_index(site, start, end) if r["case_type"] in config.CASE_TYPES]
    todo = {}
    for r in rows:
        k = r["case_number"] + r["suffix"]
        if k not in records and k not in skipped:
            todo[k] = r
    if not a.no_retry_pending:
        cutoff = (date.today() - timedelta(days=config.PENDING_MAX_DAYS)).isoformat()
        for k, p in list(pending.items()):
            if k in todo:
                continue
            if p.get("filing_date", "") < cutoff:
                p["skip_reason"] = "no_property_found"
                skipped[k] = pending.pop(k)
                continue
            todo[k] = {"case_number": p["case_number"], "suffix": p["suffix"], "case_name": p["case_name"],
                       "case_type": "ESTATE", "subtype": p["subtype"], "status": p.get("court_status", ""),
                       "opened": p["filing_date"], "closed": p.get("closed_date", "")}
    items = list(todo.items())
    if a.limit:
        items = items[: a.limit]
    log.info("%d estate cases to process (%d in index)", len(items), len(rows))

    counts = {"records": 0, "pending": 0, "skipped": 0, "errors": 0}
    for i, (k, r) in enumerate(items, 1):
        try:
            lead = enrich(site, auditor, r, a.skip_auditor)
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

    log.info("done: %s  (%d HTTP requests)", counts, http.requests_made)
    if a.dry_run:
        return 0
    save(config.RECORDS_JSON, records)
    save(config.PENDING_JSON, pending)
    save(config.SKIPPED_JSON, skipped)
    n = export_ghl_csv(records.values(), config.GHL_CSV)
    config.DOCS_DIR.mkdir(exist_ok=True)
    shutil.copyfile(config.RECORDS_JSON, config.DOCS_DIR / "records.json")
    shutil.copyfile(config.GHL_CSV, config.DOCS_DIR / config.GHL_CSV.name)
    with open(config.DOCS_DIR / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now().isoformat(timespec="minutes"), "leads": len(records),
                   "pending": len(pending), "last_run": counts}, f)
    log.info("wrote %d leads, %d pending, %d skipped, %d CSV rows", len(records), len(pending), len(skipped), n)
    return 0 if counts["errors"] < max(5, len(items) // 2) else 1


if __name__ == "__main__":
    sys.exit(main())
