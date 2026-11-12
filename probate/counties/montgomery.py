"""Montgomery County, OH (Dayton) - Probate Court case search (go.mcohio.org ColdFusion app)
+ Montgomery County Treasurer tax roll (official bulk download) for property matching.

There is no date search, so we walk estate numbers (2026EST02089, 2026EST02090 ...). Numbers
run in order within a year (~7 estates a day). Each case costs 3 requests: search -> case
detail (decedent, date of death, case type, fiduciary + address) -> docket (filing date, flags).
Low volume on purpose: new cases only, a few seconds between requests.

The court lists no decedent address, so property matches come from the decedent's name on the
tax roll, accepted only when the name points to one person (see Parcels.match).
"""
import csv
import io
import json
import logging
import re
import zipfile
from datetime import date, datetime, timedelta, timezone

from bs4 import BeautifulSoup

from .. import config, parsers
from ..auditor import Auditor, gen_suffix, name_parts, norm_street, owner_segment, same_person, summarize
from ..client import BrowserHttp
from ..common import city_state_zip, clean, to_last_first
from ..leads import finish_lead

log = logging.getLogger("probate.montgomery")

KEY, LABEL = "montgomery", "Montgomery"
BASE = "https://go.mcohio.org/applications/probate/prodcfm/"
TREASURER = "https://go.mcohio.org/applications/treasurer/search/"
REQUEST_DELAY = 3.0            # seconds between court requests
MISS_STREAK_STOP = 6           # numbers are sequential; a short run of misses = past the newest case
BACKTRACK = 15                 # re-check a few numbers below the cursor (late-posted cases)
MAX_CASES_PER_RUN = 120        # hard cap on new cases per run
YEAR_MAX = 4000                # upper bound for a year's estate numbers (~2,500 a year)
LABELS = ["Decedent's Name", "Date of Death", "Case Number", "Case Type", "Case Status",
          "Appointment date", "Attorney", "Fiduciary", "Co-Fiduciary", "Related Cases", "Balance",
          "Magistrate Name"]


def _lines(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style"]):
        t.decompose()
    return [clean(x) for x in s.get_text("\n").split("\n") if clean(x)]


def dmy(text):
    """'11-12-2026' -> '2026-11-12'."""
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", text or "")
    return f"{m[3]}-{m[1]}-{m[2]}" if m else ""


# --------------------------------------------------------------------------- parsers
def parse_search(html):
    """Links to case detail pages in a search result."""
    return re.findall(r'href="(casesearchresultx\.cfm\?[^"]+)"', html, re.I)


def parse_person(lines):
    """['O'NEAL, SHERRI J.', '7783 CILANTRO WAY, CLAYTON, OH 45315'] -> party dict."""
    if not lines:
        return {}
    name = re.sub(r"\s*-\s*[\d-]{10,}$", "", lines[0]).strip(" .")     # 'TREHERNE, JASON E. - 937-...'
    phone = (re.search(r"(\d{3}-\d{3}-\d{4})", lines[0]) or [""])[0]
    addr = " ".join(lines[1:])
    street, city, state, zip_ = "", "", "", ""
    m = re.match(r"^(.*?),\s*(.+?)[,\s]+([A-Z]{2})\s+(\d{5})", addr.upper())
    if m:
        street, (city, state, zip_) = m[1].strip(), (m[2].strip(" ,"), m[3], m[4])
    elif addr:
        street = addr
        if "," in addr:
            street, rest = addr.split(",", 1)
            city, state, zip_ = city_state_zip(rest)
    return {"name": to_last_first(name.replace(".", "")), "street": street.strip(" ,"), "city": city,
            "state": state, "zip": zip_, "phone": phone}


def parse_detail(html):
    lines = _lines(html)
    fields, cur = {}, None
    for ln in lines:
        lab = next((l for l in LABELS if ln.startswith(l)), None)
        if lab:
            cur = lab
            fields[cur] = []
            rest = ln[len(lab):].strip(" :")
            if rest:
                fields[cur].append(rest)
        elif cur:
            if ln.startswith("©"):
                break
            fields[cur].append(ln)
    g = lambda k: " ".join(fields.get(k, []))  # noqa: E731
    ctype = re.sub(r"^\d+\s*", "", g("Case Type")).upper()
    status = g("Case Status")
    docket = re.search(r'href="(CASESEARCH_DOCKETx\.cfm\?[^"]+)"', html, re.I)
    return {
        "decedent": g("Decedent's Name"), "date_of_death": parsers_mdy(g("Date of Death")),
        "case_number": g("Case Number").replace(" ", "").upper(), "case_type": ctype,
        "status": re.sub(r"\s*\d{2}-\d{2}-\d{4}.*$", "", status).upper(), "status_date": dmy(status),
        "attorney": parse_person(fields.get("Attorney", [])),
        "fiduciary": parse_person(fields.get("Fiduciary", [])),
        "co_fiduciary": parse_person(fields.get("Co-Fiduciary", [])),
        "docket_url": BASE + docket[1] if docket else "",
    }


def parsers_mdy(text):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return f"{m[3]}-{int(m[1]):02d}-{int(m[2]):02d}" if m else ""


def parse_docket(html):
    lines = _lines(html)
    try:
        lines = lines[lines.index("Description") + 1:]
    except ValueError:
        pass
    out = []
    for i, ln in enumerate(lines):
        if re.fullmatch(r"\d{2}-\d{2}-\d{4}", ln) and i + 1 < len(lines):
            out.append({"date": dmy(ln), "code": "", "description": lines[i + 1]})
    return out


def docket_flags(entries):
    f = parsers.docket_flags(entries)
    text = " | ".join(e["description"].upper() for e in entries)
    f["real_estate_summary"] = f["real_estate_summary"] or "REAL ESTATE" in text
    f["certificate_of_transfer"] = f["certificate_of_transfer"] or "CERTIFICATE OF TRANSFER" in text
    return f


# --------------------------------------------------------------------------- tax roll parcels
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _sale_ms(v):
    """'30-NOV-22' -> epoch ms (the shared summarize() expects Franklin's epoch dates)."""
    try:
        d = datetime.strptime((v or "").title(), "%d-%b-%y")
    except ValueError:
        return None
    if d.date() > date.today():
        d = d.replace(year=d.year - 100)
    return int(d.replace(tzinfo=timezone.utc).timestamp() * 1000)


def _std(r):
    """Tax-roll row -> Franklin-style keys used by auditor.classify/summarize."""
    m = re.match(r"^(.*?)\s+([A-Z]{2})\s+(\d{5})", r.get("PADDR3", ""))
    mail2 = f"{m[1]}, {m[2]} {m[3]}" if m else r.get("PADDR3", "")
    return {
        "PARCELID": r.get("PARCELID", ""), "SITEADDRESS": re.sub(r"\s+", " ", r.get("PARCELLOCATION", "")),
        "ZIPCD": r.get("PARCEL LOCATION ZIP", ""), "OWNERNME1": r.get("OWNERNAME1", ""),
        "OWNERNME2": r.get("OWNERNAME2", "") or None,
        "MAILNME1": " ".join(x for x in (r.get("PADDR1"), r.get("PADDR2")) if x), "MAILNME2": mail2,
        "CLASSDSCRP": {"R": "RESIDENTIAL", "C": "COMMERCIAL", "I": "INDUSTRIAL", "A": "AGRICULTURAL",
                       "E": "EXEMPT"}.get(r.get("CLS", ""), r.get("CLS", "")),
        "CLASSCD": r.get("LUC", ""), "TOTVALUEBASE": _num(r.get("ASMTTOTAL")),
        "SALEDATE": _sale_ms(r.get("SALESDTE")), "SALEPRICE": _num(r.get("PRICE")),
        "RENTAL": r.get("RENTALREG", ""), "RESYRBLT": int(r["YRBL"]) if (r.get("YRBL") or "").isdigit() else "",
        "RESFLRAREA_AG": int(_num(r.get("SQ. FT."))) or "", "ACRES": _num(r.get("ACRES")),
        "CVTTXDSCRP": r.get("CITYNAME", ""),
    }


class Parcels(Auditor):
    """Name lookups against the Treasurer's full tax roll, downloaded once per run when needed."""

    def __init__(self, http):
        super().__init__(http)
        self._by_last = None

    def _load(self):
        if self._by_last is not None:
            return self._by_last
        html = self.http.get_text(TREASURER + "fdpopup.cfm?dtype=TR")
        links = [l.replace("\\", "/") for l in re.findall(r"href=[\"']?(data[\\/][^\"'\s>]+\.zip)", html, re.I)]
        if not links:
            raise RuntimeError("Montgomery tax roll link not found")
        path = max(links, key=lambda l: re.sub(r"\D", "", l.rsplit("/", 1)[-1]))
        log.info("downloading tax roll %s", path)
        z = zipfile.ZipFile(io.BytesIO(self.http._send("GET", TREASURER + path).content))
        name = next(n for n in z.namelist() if n.lower().endswith((".csv", ".txt")))
        rd = csv.reader(io.TextIOWrapper(z.open(name), encoding="latin-1", newline=""))
        hdr = [h.strip() for h in next(rd)]
        idx = {}
        for row in rd:
            r = dict(zip(hdr, (c.strip() for c in row)))
            if r.get("CLS") in ("E", "U", ""):
                continue
            for o in (r.get("OWNERNAME1", ""), r.get("OWNERNAME2", "")):
                first_word = re.sub(r"[^A-Z]", "", o.split(" ")[0].upper()) if o else ""
                if first_word:
                    idx.setdefault(first_word, []).append(r)
        self._by_last = idx
        log.info("tax roll indexed: %d surnames", len(idx))
        return idx

    def by_owner(self, last, first):
        if not last or not first:
            return []
        key = re.sub(r"[^A-Z]", "", last.split(" ")[0].upper())
        seen, out = set(), []
        for r in self._load().get(key, []):
            if r["PARCELID"] not in seen:
                seen.add(r["PARCELID"])
                out.append(_std(r))
        return out

    def match(self, case_name, street, zip_code, date_of_death, fid_street=""):
        """Name-only matching (the court gives no decedent address). Accept a parcel when the
        owner names the decedent and either (a) every matching parcel belongs to one owner
        identity, or (b) the full middle name agrees, or (c) the tax bill mails to the
        fiduciary's address. Several different people with the same name -> no match."""
        last, first, middle = name_parts(case_name)
        gen = gen_suffix(case_name)
        try:
            rows = self.by_owner(last, first)
        except Exception as e:  # noqa: BLE001
            log.warning("tax roll unavailable: %s", e)
            return [], "auditor_unavailable"
        owner = lambda p: " & ".join(x for x in (p.get("OWNERNME1"), p.get("OWNERNME2")) if x)  # noqa: E731
        cands = [p for p in rows if same_person(owner(p), last, first, middle, gen)]
        people = {" ".join(owner_segment(owner(p), last, first) or []) for p in cands}
        # 'SMITH JOHN' and 'SMITH JOHN A' could be one person; 'SMITH JOHN A' vs 'SMITH JOHN B' can't
        mids = {s.split()[2][0] for s in people if len(s.split()) > 2 and s.split()[2] not in ("JR", "SR", "TR")}
        one_person = len(mids) <= 1
        fstreet = norm_street(fid_street)
        found = []
        for p in cands:
            seg = owner_segment(owner(p), last, first) or []
            full_mid = len(middle) > 1 and middle in seg
            mails_fid = bool(fstreet) and norm_street(p.get("MAILNME1") or "") == fstreet
            if full_mid or mails_fid or (one_person and len(cands) <= 4):
                sp = summarize(p, "in_decedent_name", "owner_name")
                sp["confidence"] = "high" if (full_mid or mails_fid) else "medium"
                found.append(sp)
        found.sort(key=lambda x: (x["confidence"] != "high", not str(x["class_code"]).startswith("5"),
                                  -(x["market_value"] or 0)))
        return found, ""


# --------------------------------------------------------------------------- county
class County:
    key, label = KEY, LABEL

    def __init__(self, http=None):
        self.http = http or BrowserHttp(delay=REQUEST_DELAY)
        self.parcels = Parcels(self.http)
        self.cursor_path = config.DATA_ROOT / KEY / "cursor.json"

    def _load_cursor(self):
        try:
            return json.loads(self.cursor_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_cursor(self, cur):
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(json.dumps(cur))

    def probe(self, year, num):
        """Full case for one estate number, or None if the number doesn't exist (yet)."""
        self.http._wait()
        self.http.requests_made += 1
        html = self.http._send("POST", BASE + "casesearch_actionx.cfm",
                               data={"caseyear": str(year), "casenbr": f"{num:05d}", "SEARCH": "GO"}).text
        links = parse_search(html)
        if not links:
            return None
        d = parse_detail(self.http.get_text(BASE + links[0]))
        if "EST" not in d["case_number"]:
            return None
        entries = parse_docket(self.http.get_text(d["docket_url"])) if d["docket_url"] else []
        d.update(year=year, number=num, entries=entries, detail_url=BASE + links[0],
                 filing_date=min((e["date"] for e in entries if e["date"]), default=d["status_date"]))
        return d

    def _find_start(self, year, start):
        """Binary search the first estate number of `year` filed on/after `start`."""
        lo, hi = 0, YEAR_MAX
        while hi - lo > 3:
            mid = (lo + hi) // 2
            hit = next((h for k in range(3) if (h := self.probe(year, mid + k))), None)
            if hit is None or (hit["filing_date"] or "9999") >= start.isoformat():
                hi = mid
            else:
                lo = mid
        return max(1, lo)

    def scan(self, start, end):
        cur = self._load_cursor()
        rows, n_cases = [], 0
        for year in range(start.year, end.year + 1):
            y_start = max(start, date(year, 1, 1))
            if cur.get("year") == year and cur.get("date", "") >= (y_start - timedelta(days=3)).isoformat():
                n = max(1, cur["number"] - BACKTRACK)
            else:
                log.info("locating first %d estate filed on/after %s ...", year, y_start)
                n = self._find_start(year, y_start)
            log.info("walking %d estate numbers from %d", year, n)
            misses = 0
            while misses < MISS_STREAK_STOP and n_cases < MAX_CASES_PER_RUN:
                hit = self.probe(year, n)
                if hit is None:
                    misses += 1
                else:
                    misses = 0
                    fd = hit["filing_date"]
                    if year > cur.get("year", 0) or n >= cur.get("number", 0):
                        cur = {"year": year, "number": n, "date": fd or cur.get("date", "")}
                    if start.isoformat() <= (fd or end.isoformat()) <= end.isoformat():
                        rows.append(self._row(hit))
                        n_cases += 1
                n += 1
        self._save_cursor(cur)
        log.info("montgomery: %d estate cases in range, cursor now %s", len(rows), cur)
        return rows

    @staticmethod
    def _row(h):
        return {"case_number": h["case_number"], "suffix": "", "case_name": to_last_first(h["decedent"]),
                "case_type": "ESTATE", "subtype": h["case_type"], "status": h["status"],
                "opened": h["filing_date"], "closed": h["status_date"] if h["status"] == "CLOSED" else "",
                "q": h["detail_url"], "_hit": h}

    def enrich(self, row, skip_auditor=False):
        h = row.get("_hit")
        if h is None:                                   # pending lead from an earlier run
            h = self.probe(int(row["case_number"][:4]), int(re.sub(r"\D", "", row["case_number"][7:])))
            if h is None:
                raise RuntimeError("case not found")
        fid = dict(h["fiduciary"], title="FIDUCIARY") if h["fiduciary"] else {}
        last, first, middle = name_parts(row["case_name"])
        lead = {
            "case_key": row["case_number"], "case_number": row["case_number"], "suffix": "",
            "lead_type": "Probate", "county": KEY, "case_name": row["case_name"],
            "decedent_last": last.title(), "decedent_first": first.title(), "decedent_middle": middle.title(),
            "aka": "", "subtype": row["subtype"], "court_status": row["status"],
            "filing_date": row["opened"], "closed_date": row["closed"],
            "date_of_death": h["date_of_death"], "decedent_street": "", "decedent_city": "",
            "decedent_state": "", "decedent_zip": "", "fiduciary": fid,
            "fiduciaries": [dict(p, role=r) for r, p in (("FIDUCIARY", h["fiduciary"]),
                                                         ("CO-FIDUCIARY", h["co_fiduciary"])) if p],
            "attorney": h["attorney"], "docket_flags": docket_flags(h["entries"]), "q": h["detail_url"],
            "court_url": h["detail_url"], "last_checked": datetime.now().isoformat(timespec="seconds"),
        }
        return self._match(lead, skip_auditor)

    def _match(self, lead, skip_auditor):
        if skip_auditor:
            return finish_lead(lead, [], "skipped")
        props, err = self.parcels.match(lead["case_name"], "", "", lead["date_of_death"],
                                        (lead.get("fiduciary") or {}).get("street", ""))
        return finish_lead(lead, props, err)

    def rematch(self, lead):
        """Pending retry: only the tax-roll lookup is repeated."""
        lead = dict(lead)
        lead["last_checked"] = datetime.now().isoformat(timespec="seconds")
        return self._match(lead, False)
