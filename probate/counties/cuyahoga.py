"""Cuyahoga County, OH - Probate Court web docket (PROWARE, probate.cuyahogacounty.gov/pa)
+ Cuyahoga County Fiscal Officer open-data parcels.

The docket has no date search, so we walk estate case numbers (2026EST311200 ...). Numbers
are shared with other case types (~50% are estates), so a run stops after a long streak of
misses. The last number seen is kept in data/cuyahoga/cursor.json.

Per case: search by number -> results grid -> postback to Case Summary (filing date, type)
-> Case Parties (decedent DOD + address, applicant/fiduciary address) -> Case Docket.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

from bs4 import BeautifulSoup

from .. import config, parsers
from ..auditor import Auditor, name_parts
from ..client import Http
from ..common import city_state_zip, clean, mdy, pick_contact, same_street, street_core, to_last_first
from ..leads import finish_lead

log = logging.getLogger("probate.cuyahoga")

KEY, LABEL = "cuyahoga", "Cuyahoga"
BASE = "https://probate.cuyahogacounty.gov/pa/"
PARCELS = "https://gis.cuyahogacounty.gov/server/rest/services/Open_Data_Parcels/MapServer"
PARCEL_LAYERS = (0, 1)            # 0 = Cleveland, 1 = rest of county
PARCEL_FIELDS = ("parcelpin,deeded_owner,transfer_date,sales_amount,par_addr,par_predir,par_street,"
                 "par_suffix,par_unit,par_city,par_zip,mail_name,mail_addr_street,mail_city,mail_state,"
                 "mail_zip,tax_luc,tax_luc_description,certified_tax_total,total_res_liv_area,total_acreage")
MISS_STREAK_STOP = 60             # consecutive non-estate numbers before we assume we're past the end
BACKTRACK = 40                    # re-check this many numbers below the cursor each run
SEED = 300000                     # an estate number safely before 2026 (2026 starts ~304xxx)
ROLE_FIX = {"APPLICANT1": "APPLICANT", "A1": "APPLICANT"}


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _form_fields(soup):
    """All named inputs/selects of the page's ASP.NET form (minus buttons)."""
    out = {}
    for el in soup.select("form input, form select"):
        name = el.get("name")
        if not name or el.get("type") in ("submit", "radio", "button", "image", "checkbox"):
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            out[name] = opt.get("value", "") if opt else ""
        else:
            out[name] = el.get("value", "")
    return out


# --------------------------------------------------------------------------- parsers
def parse_results(html):
    """Search results grid -> (rows, first postback target). rows: name, case, address, role."""
    s = _soup(html)
    if "No results were found" in s.get_text():
        return [], None
    rows, target = [], None
    grid = s.find(id="mpContentPH_gvSearchResults")
    for tr in (grid.find_all("tr") if grid else []):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        a = tds[0].find("a", href=True)
        m = re.search(r"__doPostBack\('([^']+)'", a["href"]) if a else None
        if m and target is None:
            target = m.group(1)
        rows.append({"name": clean(tds[0].get_text()), "case": clean(tds[1].get_text()),
                     "address": clean(tds[2].get_text()), "role": clean(tds[3].get_text()).upper()})
    return rows, target


def parse_summary(html):
    s = _soup(html)
    g = lambda i: clean(s.find(id=i).get_text()) if s.find(id=i) else ""  # noqa: E731
    q = None
    for a in s.find_all("a", href=True):
        m = re.search(r"Case(?:Summary|Parties|Docket)\.aspx\?q=([\w-]+)", a["href"])
        if m:
            q = m.group(1)
            break
    return {"case_full": g("mpContentPH_txtCaseNum"), "title": g("mpContentPH_txtCaseTitle"),
            "case_type": g("mpContentPH_txtCaseType").upper(), "filing_date": mdy(g("mpContentPH_txtCaseInit")),
            "status": g("mpContentPH_txtCaseStatus"), "status_date": mdy(g("mpContentPH_txtStatusDate")), "q": q}


def parse_parties(html):
    s = _soup(html)
    out = []
    for role_el in s.find_all("span", id=re.compile(r"mpContentPH_ctl\d+_lblPartyRole$")):
        pre = role_el["id"][: -len("lblPartyRole")]
        g = lambda k: clean(s.find(id=pre + k).get_text()) if s.find(id=pre + k) else ""  # noqa: E731
        name = g("lblName")
        dod = ""
        m = re.search(r"\(DOD:\s*([\d/]+)\)", name)
        if m:
            dod, name = mdy(m.group(1)), clean(name[: m.start()])
        lines = [g(f"lblAddress{i}") for i in range(1, 8)]
        lines = [x for x in lines if x]
        street, city, state, zip_ = "", "", "", ""
        if lines:
            csz_i = next((i for i, x in enumerate(lines) if re.search(r"\b[A-Z]{2}\s+\d{5}", x.upper())), None)
            if csz_i is not None:
                street = " ".join(lines[:csz_i])
                city, state, zip_ = city_state_zip(lines[csz_i])
            else:
                street = " ".join(lines)
        role = clean(role_el.get_text()).upper()
        out.append({"role": ROLE_FIX.get(role, role), "name": name, "dod": dod, "street": street,
                    "city": city, "state": state, "zip": zip_})
    return out


def parse_docket(html):
    s = _soup(html)
    out = []
    for i in range(0, 500):
        d = s.find(id=f"mpContentPH_gvDocket_lblDocketDate_{i}")
        if d is None:
            break
        t = s.find(id=f"mpContentPH_gvDocket_lblDocket_{i}")
        out.append({"date": mdy(d.get_text()), "code": "", "description": clean(t.get_text() if t else "")})
    return out


def docket_flags(entries):
    f = parsers.docket_flags(entries)
    text = " | ".join(e["description"].upper() for e in entries)
    f["real_estate_summary"] = f["real_estate_summary"] or "REAL ESTATE" in text
    f["certificate_of_transfer"] = f["certificate_of_transfer"] or "TRANSFER OF REAL ESTATE" in text
    return f


# --------------------------------------------------------------------------- site client
class Site:
    def __init__(self, http):
        self.http = http
        self.search_fields = None

    def _post(self, url, data):
        self.http._wait()
        self.http.requests_made += 1
        r = self.http.s.post(url, data=data, timeout=config.TIMEOUT_SEC)
        r.raise_for_status()
        return r.text

    def _get(self, url):
        return self.http.get_text(url)

    def start(self):
        """Accept the Conditions of Use (user-approved) and load the search form."""
        s = _soup(self._get(BASE))
        if s.find(id="mpContentPH_btnYes"):
            data = _form_fields(s)
            data["ctl00$mpContentPH$btnYes"] = "Yes"
            s = _soup(self._post(BASE, data))
        if not s.find(id="mpContentPH_txtCaseNum"):
            s = _soup(self._get(BASE + "CaseSearch.aspx"))
        if not s.find(id="mpContentPH_txtCaseNum"):
            raise RuntimeError("Cuyahoga case search form not found (terms page changed?)")
        self.search_fields = _form_fields(s)

    def search_number(self, year, num):
        if self.search_fields is None:
            self.start()
        data = dict(self.search_fields)
        data.update({"ctl00$mpContentPH$txtCaseYear": str(year), "ctl00$mpContentPH$ddlCaseCat": "EST",
                     "ctl00$mpContentPH$txtCaseNum": str(num),
                     "ctl00$mpContentPH$btnSearchByCase": "Search By Case Number"})
        html = self._post(BASE + "CaseSearch.aspx", data)
        if "mpContentPH_gvSearchResults" not in html and "A case number is required" not in html:
            self.start()                                   # session/terms expired: retry once
            data.update({k: v for k, v in self.search_fields.items() if k.startswith("__")})
            html = self._post(BASE + "CaseSearch.aspx", data)
        return html

    def open_case(self, results_html, target):
        s = _soup(results_html)
        data = _form_fields(s)
        data.update({"__EVENTTARGET": target, "__EVENTARGUMENT": ""})
        return self._post(BASE + "SearchResults.aspx", data)

    def page(self, name, q):
        return self._get(f"{BASE}{name}.aspx?q={q}")


# --------------------------------------------------------------------------- parcels
def _std(a):
    """Cuyahoga parcel attributes -> Franklin-style keys used by auditor.classify/summarize."""
    addr = " ".join(x for x in (str(a.get("par_addr") or ""), a.get("par_predir"), a.get("par_street"),
                                a.get("par_suffix")) if x)
    if a.get("par_unit"):
        addr += f" UNIT {a['par_unit']}"
    return {
        "PARCELID": a.get("parcelpin", ""), "SITEADDRESS": addr, "ZIPCD": str(a.get("par_zip") or ""),
        "OWNERNME1": (a.get("deeded_owner") or "").replace(",", " "), "OWNERNME2": None,
        "MAILNME1": a.get("mail_addr_street") or "",
        "MAILNME2": f"{a.get('mail_city') or ''}, {a.get('mail_state') or ''} {a.get('mail_zip') or ''}".upper(),
        "CLASSDSCRP": a.get("tax_luc_description") or "", "CLASSCD": a.get("tax_luc") or "",
        "TOTVALUEBASE": a.get("certified_tax_total") or 0, "SALEDATE": a.get("transfer_date"),
        "SALEPRICE": a.get("sales_amount") or 0, "RESFLRAREA_AG": a.get("total_res_liv_area") or "",
        "ACRES": a.get("total_acreage") or 0, "CVTTXDSCRP": a.get("par_city") or "",
    }


class Parcels(Auditor):
    def _query(self, where, limit=25):
        rows = []
        for layer in PARCEL_LAYERS:
            params = {"where": where, "outFields": PARCEL_FIELDS, "returnGeometry": "false",
                      "resultRecordCount": limit, "f": "json"}
            try:
                j = self.http.get_json(f"{PARCELS}/{layer}/query", params)
            except Exception as e:  # noqa: BLE001
                log.warning("parcel query failed (%s): %s", where, e)
                return None
            if "error" in j:
                log.warning("parcel error %s for %s", j["error"], where)
                return None
            rows += [_std(f["attributes"]) for f in j.get("features", [])]
        return rows

    def by_address(self, street, zip_code=""):
        num, core = street_core(street)
        if not num or not core:
            return [], True
        word = max(core.split(), key=len).replace("'", "''")      # 'NORTH TAYLOR' vs 'N. TAYLOR'
        rows = self._query(f"par_addr = '{num}' AND par_street LIKE '%{word}%'")
        if rows is None:
            return None, True
        rows = [r for r in rows if same_street(r["SITEADDRESS"], street)]
        if zip_code[:5].isdigit() and len(rows) > 1:
            rows = [r for r in rows if r["ZIPCD"][:5] == zip_code[:5]] or rows
        return rows, len(rows) <= 1

    def by_owner(self, last, first):
        if not last or not first:
            return []
        l, f = last.replace("'", "''"), first.replace("'", "''")
        return self._query(f"deeded_owner LIKE '{l}, {f}%' OR deeded_owner LIKE '{l} {f}%'") or []


# --------------------------------------------------------------------------- county
class County:
    key, label = KEY, LABEL

    def __init__(self, http=None):
        self.http = http or Http()
        self.site = Site(self.http)
        self.parcels = Parcels(self.http)
        self.cursor_path = config.DATA_ROOT / KEY / "cursor.json"

    # ---- cursor
    def _load_cursor(self):
        try:
            return json.loads(self.cursor_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cursor(self, cur):
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(json.dumps(cur))

    def _probe(self, num, years):
        """Return (summary, results_rows, results_html) for an estate number, or None."""
        for y in years:
            html = self.site.search_number(y, num)
            rows, target = parse_results(html)
            if rows and target:
                summ = parse_summary(self.site.open_case(html, target))
                summ.update({"number": num, "year": y, "result_rows": rows})
                return summ
        return None

    def _years_for(self, d):
        return [d.year, d.year - 1] if d.month == 1 else [d.year]

    def _find_start(self, start, hi):
        """Binary search the first estate number filed on/after `start`."""
        lo = SEED
        years = [start.year, start.year - 1]               # numbers just before `start` may be last year's
        for _ in range(22):
            if hi - lo <= 30:
                break
            mid = (lo + hi) // 2
            hit = None
            for k in range(8):                              # nearest estate at/after mid
                hit = self._probe(mid + k, years)
                if hit:
                    break
            if not hit or not hit["filing_date"]:
                hi = mid
                continue
            if hit["filing_date"] < start.isoformat():
                lo = mid
            else:
                hi = mid
        return lo

    def scan(self, start, end):
        cur = self._load_cursor()
        if cur.get("number") and cur.get("date", "") >= (start - timedelta(days=3)).isoformat():
            n = cur["number"] - BACKTRACK
        else:
            hi = cur.get("number") or SEED + 20000
            log.info("locating first estate filed on/after %s ...", start)
            n = self._find_start(start, hi + 2000)
        log.info("walking estate numbers from %d", n)
        rows, misses, best = [], 0, dict(cur)
        while misses < MISS_STREAK_STOP:
            d = date.fromisoformat(best.get("date") or start.isoformat())
            years = sorted({d.year, end.year}, reverse=True) if d.month == 12 or d.year != end.year \
                else self._years_for(d)
            hit = self._probe(n, years)
            if hit is None:
                misses += 1
            else:
                misses = 0
                fd = hit["filing_date"] or ""
                if n >= best.get("number", 0):
                    best = {"number": n, "date": fd or best.get("date", "")}
                if start.isoformat() <= fd <= end.isoformat():
                    rows.append(self._row(hit))
            n += 1
        self._save_cursor(best)
        log.info("cuyahoga: %d estate cases in range, cursor now %s", len(rows), best)
        return rows

    @staticmethod
    def _row(h):
        dec = next((r for r in h["result_rows"] if r["role"] == "DECEDENT"), None)
        name = to_last_first(dec["name"]) if dec else re.sub(r"^THE ESTATE OF\s+", "", h["title"]).replace(".", "")
        return {"case_number": h["case_full"] or f"{h['year']}EST{h['number']}", "suffix": "",
                "case_name": to_last_first(name), "case_type": "ESTATE", "subtype": h["case_type"],
                "status": h["status"], "opened": h["filing_date"],
                "closed": h["status_date"] if h["status"].upper() == "CLOSED" else "", "q": h["q"]}

    def enrich(self, row, skip_auditor=False):
        q = row.get("q")
        parties = parse_parties(self.site.page("CaseParties", q)) if q else []
        try:
            flags = docket_flags(parse_docket(self.site.page("CaseDocket", q))) if q else {}
        except Exception as e:  # noqa: BLE001
            log.warning("%s docket: %s", row["case_number"], e)
            flags = {}
        dec = next((p for p in parties if p["role"] == "DECEDENT"), {})
        contact = pick_contact(parties)
        atty = next((p for p in parties if p["role"] == "ATTORNEY"), {})
        fid = {"name": to_last_first(contact.get("name")), "title": contact.get("role", ""),
               "street": contact.get("street", ""), "city": contact.get("city", ""),
               "state": contact.get("state", ""), "zip": contact.get("zip", ""), "phone": ""} if contact else {}
        lead = self._base_lead(row, dec, fid, parties, atty, flags)
        return self._match(lead, skip_auditor)

    def _base_lead(self, row, dec, fid, parties, atty, flags):
        last, first, middle = name_parts(row["case_name"])
        return {
            "case_key": row["case_number"], "case_number": row["case_number"], "suffix": "",
            "lead_type": "Probate", "county": KEY, "case_name": row["case_name"],
            "decedent_last": last.title(), "decedent_first": first.title(), "decedent_middle": middle.title(),
            "aka": "", "subtype": row["subtype"], "court_status": row["status"],
            "filing_date": row["opened"], "closed_date": row["closed"],
            "date_of_death": dec.get("dod", ""), "decedent_street": dec.get("street", ""),
            "decedent_city": dec.get("city", ""), "decedent_state": dec.get("state", ""),
            "decedent_zip": dec.get("zip", ""), "fiduciary": fid,
            "fiduciaries": [p for p in parties if p["role"] not in ("DECEDENT", "ATTORNEY")][:4],
            "attorney": {"name": to_last_first(atty.get("name")), "street": atty.get("street", ""),
                         "city": atty.get("city", ""), "state": atty.get("state", ""), "zip": atty.get("zip", ""),
                         "phone": ""} if atty else {},
            "docket_flags": flags, "q": row.get("q"),
            "court_url": f"{BASE}CaseSummary.aspx?q={row.get('q')}" if row.get("q") else BASE,
            "last_checked": datetime.now().isoformat(timespec="seconds"),
        }

    def _match(self, lead, skip_auditor):
        props, err = ([], "skipped") if skip_auditor else self.parcels.match(
            lead["case_name"], lead["decedent_street"], lead["decedent_zip"], lead["date_of_death"])
        return finish_lead(lead, props, err)

    def rematch(self, lead):
        """Pending retry: only the parcel lookup is repeated."""
        lead = dict(lead)
        lead["last_checked"] = datetime.now().isoformat(timespec="seconds")
        return self._match(lead, False)
