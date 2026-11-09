"""Franklin County, OH - Probate Court Net.Data site + Franklin County Auditor parcels."""
import logging
from datetime import datetime

from .. import config, parsers
from ..auditor import Auditor, name_parts
from ..client import Http, ProbateSite
from ..leads import finish_lead

log = logging.getLogger("probate.franklin")

KEY = "franklin"
LABEL = "Franklin"


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




def enrich(site, auditor, row, skip_auditor=False):  # noqa: C901
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
        "county": "franklin",
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
        "last_checked": datetime.now().isoformat(timespec="seconds"),
    }
    return finish_lead(lead, props, err)




class County:
    key, label = KEY, LABEL

    def __init__(self, http=None):
        self.http = http or Http()
        self.site, self.auditor = ProbateSite(self.http), Auditor(self.http)

    def scan(self, start, end):
        return [r for r in scan_index(self.site, start, end) if r["case_type"] in config.CASE_TYPES]

    def enrich(self, row, skip_auditor=False):
        return enrich(self.site, self.auditor, row, skip_auditor)
