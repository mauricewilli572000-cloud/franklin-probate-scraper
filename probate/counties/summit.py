"""Summit County, OH - Probate Court CourtView eServices (search.summitohioprobate.com)
+ Summit County Fiscal Office parcels (scgis.summitoh.net).

The "Case Type" search tab accepts a case type + file-date range with no name, which lists
every party of every case filed in the window. Each case's detail page (plain GET, no JS)
has the decedent's DOD + address, the applicant/fiduciary address and the attorney.

Shelby County runs the same CourtView product (search.shelbyco.net) but puts a CAPTCHA in
front of it, so it is not scraped.
"""
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from .. import config, parsers
from ..auditor import Auditor, name_parts
from ..client import BrowserHttp, Http
from ..common import city_state_zip, clean, mdy, pick_contact, same_street, street_core
from ..leads import finish_lead

log = logging.getLogger("probate.summit")

KEY, LABEL = "summit", "Summit"
BASE = "https://search.summitohioprobate.com/eservices/"
CASE_CODES = ("ES", "ER")                 # Estate, Release of Administration
WINDOW_DAYS = 1                           # results page holds ~50 party rows and "Next" is an AJAX
                                          # button we can't follow, so search one type + one day at a time
PARCELS = "https://scgis.summitoh.net/hosted/rest/services/parcels_cama/parcels_Esri_ArcMap_Static/MapServer/0/query"
SALES = "https://scgis.summitoh.net/hosted/rest/services/parcels_cama/Tax_Parcel_Sales/MapServer/0/query"
PARCEL_FIELDS = "PARID,ADDR,OWN1,OWN2,OWNADDR1,OWNADDR2,OWNADDR3,OWNZIP1,CITYNAME,LUC,CLASS,BLDGSF,ACRES"
SALES_FIELDS = "parcelid,transdt,saleamnt,cntmktvalue,resyrblt,resflrarea,resstrtypeval,grantee"
LUC_NAMES = {"500": "RESIDENTIAL VACANT LAND", "510": "SINGLE FAMILY", "511": "SINGLE FAMILY 10+ AC",
             "520": "TWO FAMILY", "530": "THREE FAMILY", "550": "CONDOMINIUM", "560": "HOUSEBOAT/MOBILE",
             "599": "OTHER RESIDENTIAL", "401": "APARTMENTS 4-19 UNITS", "402": "APARTMENTS 20-39 UNITS"}


def _soup(html):
    return BeautifulSoup(html, "html.parser")


# --------------------------------------------------------------------------- parsers
def parse_results(html, page_url):
    """Results grid -> {case_number: {case, parties:[{name,role}], type, file_date, action, status, href}}."""
    s = _soup(html)
    table = next((t for t in s.find_all("table")
                  if "Party/Company" in t.get_text() and "Case Number" in t.get_text()), None)
    cases, next_href = {}, None
    if table is None:
        return cases, None
    truncated = any("Next" in a.get_text() for a in s.find_all("a"))
    heads = [clean(th.get_text()) for th in table.find_all("th")]
    idx = {h: i for i, h in enumerate(heads)}
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < len(heads):
            continue
        cell = lambda h: clean(tds[idx[h]].get_text()) if h in idx else ""  # noqa: E731
        cn = cell("Case Number")
        if not re.match(r"\d{4} [A-Z]{2} \d{5}", cn):
            continue
        a = tds[idx["Case Number"]].find("a", href=True)
        c = cases.setdefault(cn, {"case": cn, "parties": [], "type": cell("Case Type"),
                                  "file_date": mdy(cell("File Date")), "action": cell("Initiating Action"),
                                  "status": cell("Case Status"),
                                  "href": urljoin(page_url, a["href"]) if a else None})
        c["parties"].append({"name": cell("Party/Company"), "role": cell("Party Type").upper()})
    for a in s.find_all("a", href=True):
        if "Next" in a.get_text() and "?x=" in a["href"]:
            next_href = urljoin(page_url, a["href"])
    if truncated and not next_href:
        next_href = "TRUNCATED"
    return cases, next_href


def parse_case(html):
    s = _soup(html)
    info = {}
    for ul in s.select(".caseInfo ul"):
        lab, val = ul.select_one(".caseHdrLabel"), ul.select_one(".caseHdrInfo")
        if lab and val:
            info[clean(lab.get_text()).rstrip(":").lower()] = clean(val.get_text())
    title = clean(s.select_one("h1.caseHdrTitle").get_text()) if s.select_one("h1.caseHdrTitle") else ""
    parties = []
    box = s.find(id="ptyContainer")
    for row in (box.find_all("div", class_=re.compile(r"^row(odd|even)$"), recursive=False) if box else []):
        name = clean(row.select_one(".pty-name").get_text()) if row.select_one(".pty-name") else ""
        role = clean(row.select_one(".pty-cd").get_text()).lstrip("- ").upper() if row.select_one(".pty-cd") else ""
        dod = ""
        for li in row.select(".pty-personalInfo ul ul"):
            lab, val = li.select_one(".ptyPersLabel"), li.select_one(".ptyPersInfo")
            if lab and val and clean(lab.get_text()) == "DOD":
                dod = mdy(val.get_text())
        lines = [clean(d.get_text()) for d in row.select(".ptyContactInfo .addrLn1, .ptyContactInfo .addrLn2, "
                                                         ".ptyContactInfo .addrLn3")]
        csz = row.select_one(".ptyContactInfo .state-city-zip")
        city, state, zip_ = city_state_zip(csz.get_text()) if csz else ("", "", "")
        atty = [clean(x.get_text()) for x in row.select(".ptyAttyInfo")]
        parties.append({"role": role, "name": name.upper(), "dod": dod,
                        "street": " ".join(x for x in lines if x).upper(), "city": city, "state": state,
                        "zip": zip_, "attorney": atty[0].upper() if atty else ""})
    docket = []
    t = next((t for t in s.find_all("table") if "Docket Text" in t.get_text()), None)
    for tr in (t.select("tbody tr") if t else []):
        tds = tr.find_all("td")
        if len(tds) >= 2:
            docket.append({"date": mdy(tds[0].get_text()), "code": "", "description": clean(tds[1].get_text())})
    return {"title": title, "case_type": info.get("case type", ""), "status": info.get("case status", ""),
            "file_date": mdy(info.get("file date", "")), "action": info.get("action", "").upper(),
            "status_date": mdy(info.get("status date", "")), "parties": parties, "docket": docket}


# --------------------------------------------------------------------------- site client
class Site:
    def __init__(self, http):
        self.http = http

    def _req(self, method, url, **kw):
        self.http._wait()
        self.http.requests_made += 1
        if hasattr(self.http, "_send"):                       # BrowserHttp (curl_cffi) with retries
            r = self.http._send(method, url, **kw)
        else:
            r = self.http.s.request(method, url, timeout=config.TIMEOUT_SEC, **kw)
            r.raise_for_status()
        return r.text, str(r.url)

    def _get(self, url):
        return self._req("GET", url)

    def _post(self, url, data):
        # repeated keys (caseCd) -> encode ourselves so both HTTP clients send the same body
        return self._req("POST", url, data=urlencode(data),
                         headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url})

    def search(self, start, end, codes=CASE_CODES):
        """All cases of `codes` filed start..end (dates). Returns parse_results dict."""
        tab = None
        for path in ("home.page", "casesearch", "search.page"):     # same path a browser takes
            html, url = self._get(BASE + path)
            s = _soup(html)
            log.info("summit GET %s -> %s len=%d title=%r cookies=%s scripts=%s", path, url, len(html),
                     clean(s.title.get_text()) if s.title else "", list(getattr(self.http.s, "cookies", {}).keys()),
                     [x.get("src") for x in s.find_all("script") if x.get("src")][:6])
            if path == "home.page":
                log.info("summit home.page html: %s", re.sub(r"\s+", " ", html)[:4900])
            tab = next((a for a in s.find_all("a", href=True) if clean(a.get_text()) == "Case Type"), None)
            if tab is not None:
                break
        if tab is None:
            title = clean(s.title.get_text()) if s.title else ""
            links = [f"{clean(a.get_text())[:30]}=>{a['href'][:60]}" for a in s.find_all("a", href=True)][:25]
            log.error("summit page %s title=%r text=%r links=%s", url, title, clean(s.get_text())[:800], links)
            raise RuntimeError("Summit: 'Case Type' search tab not found")
        html, url = self._get(urljoin(url, tab["href"]))
        s = _soup(html)
        btn = s.find("input", attrs={"name": "submitLink"})
        form = btn.find_parent("form") if btn else None
        if form is None:
            raise RuntimeError("Summit: case-type search form not found")
        data = []
        for el in form.find_all(["input", "select"]):
            n = el.get("name")
            if not n or n in ("caseCd", "submitLink"):
                continue
            if el.name == "select":
                if el.has_attr("multiple"):
                    continue
                opt = el.find("option", selected=True) or el.find("option")
                data.append((n, opt.get("value", "") if opt else ""))
            elif el.get("type") not in ("checkbox", "radio", "submit", "button"):
                data.append((n, el.get("value", "")))
        sel = form.find("select", attrs={"name": "caseCd"})
        for o in sel.find_all("option"):
            if o.get("value", "").strip() in codes:
                data.append(("caseCd", o["value"]))
        data = [(k, v) for k, v in data if not k.startswith("fileDateRange")]
        data += [("fileDateRange:dateInputBegin", start.strftime("%m/%d/%Y")),
                 ("fileDateRange:dateInputEnd", end.strftime("%m/%d/%Y")), ("submitLink", "Search")]
        html, url = self._post(urljoin(url, form["action"]), data)
        if "Last Name or Company Name is required" in html:
            raise RuntimeError("Summit: search form now requires a name")
        cases, nxt = parse_results(html, url)
        if nxt == "TRUNCATED":
            log.warning("summit %s %s..%s: more results than one page - some cases missed", codes, start, end)
            nxt = None
        pages = 1
        while nxt and pages < 30:
            html, url = self._get(nxt)
            more, nxt = parse_results(html, url)
            for k, v in more.items():
                if k in cases:
                    cases[k]["parties"] += v["parties"]
                else:
                    cases[k] = v
            pages += 1
        return cases

    def case(self, href):
        return self._get(href)[0]


# --------------------------------------------------------------------------- parcels
def _std(a, sale=None):
    sale = sale or {}
    addr = re.sub(r"\s+", " ", a.get("ADDR") or "").strip()
    mail2 = " ".join(x for x in (a.get("OWNADDR2"), a.get("OWNADDR3")) if x)
    return {
        "PARCELID": a.get("PARID", ""), "SITEADDRESS": addr, "ZIPCD": "",
        "OWNERNME1": (a.get("OWN1") or "").strip(), "OWNERNME2": (a.get("OWN2") or "").strip() or None,
        "MAILNME1": (a.get("OWNADDR1") or "").strip(),
        "MAILNME2": f"{mail2} {a.get('OWNZIP1') or ''}".strip(),
        "CLASSDSCRP": sale.get("resstrtypeval") or LUC_NAMES.get(str(a.get("LUC") or ""), ""),
        "CLASSCD": a.get("LUC") or "", "TOTVALUEBASE": sale.get("cntmktvalue") or 0,
        "SALEDATE": sale.get("transdt"), "SALEPRICE": sale.get("saleamnt") or 0,
        "RESYRBLT": sale.get("resyrblt") or "", "RESFLRAREA_AG": sale.get("resflrarea") or a.get("BLDGSF") or "",
        "ACRES": a.get("ACRES") or 0, "CVTTXDSCRP": (a.get("CITYNAME") or "").strip(),
    }


class Parcels(Auditor):
    def _raw(self, url, where, fields, limit=25):
        params = {"where": where, "outFields": fields, "returnGeometry": "false",
                  "resultRecordCount": limit, "f": "json"}
        try:
            j = self.http.get_json(url, params)
        except Exception as e:  # noqa: BLE001
            log.warning("parcel query failed (%s): %s", where, e)
            return None
        if "error" in j:
            log.warning("parcel error %s for %s", j["error"], where)
            return None
        return [f["attributes"] for f in j.get("features", [])]

    def _query(self, where, limit=25):
        rows = self._raw(PARCELS, where, PARCEL_FIELDS, limit)
        if not rows:
            return rows
        ids = ",".join("'" + r["PARID"] + "'" for r in rows if r.get("PARID"))
        sales = {s["parcelid"]: s for s in (self._raw(SALES, f"parcelid IN ({ids})", SALES_FIELDS, 100) or [])}
        return [_std(r, sales.get(r.get("PARID"))) for r in rows]

    def by_address(self, street, zip_code=""):
        num, core = street_core(street)
        if not num or not core:
            return [], True
        word = max(core.split(), key=len).replace("'", "''")
        rows = self._query(f"ADDR LIKE '{num} %{word}%'")
        if rows is None:
            return None, True
        rows = [r for r in rows if same_street(r["SITEADDRESS"], street)]
        return rows, len(rows) <= 1

    def by_owner(self, last, first):
        if not last or not first:
            return []
        l, f = last.replace("'", "''"), first.replace("'", "''")
        return self._query(f"OWN1 LIKE '{l} {f}%' OR OWN2 LIKE '{l} {f}%'") or []


# --------------------------------------------------------------------------- county
class County:
    key, label = KEY, LABEL

    def __init__(self, http=None):
        if http is None:
            try:
                http = BrowserHttp()      # Summit resets Python's default TLS handshake
            except ImportError:
                log.warning("curl_cffi not installed - falling back to requests (likely blocked)")
                http = Http()
        self.http = http
        self.site = Site(self.http)
        self.parcels = Parcels(self.http)

    def scan(self, start, end):
        rows, d = [], start
        while d <= end:
            w_end = min(end, d + timedelta(days=WINDOW_DAYS - 1))
            cases = {}
            for code in CASE_CODES:
                cases.update(self.site.search(d, w_end, (code,)))
            for c in cases.values():
                dec = next((p for p in c["parties"] if p["role"] == "DECEDENT"), None)
                if not dec:
                    continue
                key = c["case"].replace(" ", "")
                rows.append({"case_number": key, "suffix": "", "case_name": dec["name"].upper(),
                             "case_type": "ESTATE", "subtype": c["action"].upper(), "status": c["status"],
                             "opened": c["file_date"], "closed": "", "href": c["href"],
                             "case_display": c["case"]})
            log.info("summit %s..%s: %d cases", d, w_end, len(cases))
            d = w_end + timedelta(days=1)
        return rows

    def enrich(self, row, skip_auditor=False):
        case = parse_case(self.site.case(row["href"])) if row.get("href") else {"parties": [], "docket": []}
        parties = case["parties"]
        dec = next((p for p in parties if p["role"] == "DECEDENT"), {})
        contact = pick_contact(parties)
        atty_name = contact.get("attorney") or next((p["attorney"] for p in parties if p.get("attorney")), "")
        last, first, middle = name_parts(row["case_name"])
        lead = {
            "case_key": row["case_number"], "case_number": row["case_number"], "suffix": "",
            "lead_type": "Probate", "county": KEY, "case_name": row["case_name"],
            "decedent_last": last.title(), "decedent_first": first.title(), "decedent_middle": middle.title(),
            "aka": "", "subtype": (case.get("action") or row["subtype"]).upper(),
            "court_status": case.get("status") or row["status"], "filing_date": row["opened"] or case.get("file_date"),
            "closed_date": case.get("status_date") if (case.get("status") or "").upper() == "CLOSED" else "",
            "date_of_death": dec.get("dod", ""), "decedent_street": dec.get("street", ""),
            "decedent_city": dec.get("city", ""), "decedent_state": dec.get("state", ""),
            "decedent_zip": dec.get("zip", ""),
            "fiduciary": {"name": contact.get("name", ""), "title": contact.get("role", ""),
                          "street": contact.get("street", ""), "city": contact.get("city", ""),
                          "state": contact.get("state", ""), "zip": contact.get("zip", ""), "phone": ""}
            if contact else {},
            "fiduciaries": [p for p in parties if p["role"] != "DECEDENT"][:4],
            "attorney": {"name": atty_name, "phone": ""} if atty_name else {},
            "docket_flags": parsers.docket_flags(case["docket"]) if case["docket"] else {},
            "case_display": row.get("case_display", row["case_number"]),
            "court_url": BASE + "search.page",
            "last_checked": datetime.now().isoformat(timespec="seconds"),
        }
        return self._match(lead, skip_auditor)

    def _match(self, lead, skip_auditor):
        props, err = ([], "skipped") if skip_auditor else self.parcels.match(
            lead["case_name"], lead["decedent_street"], lead["decedent_zip"], lead["date_of_death"])
        return finish_lead(lead, props, err)

    def rematch(self, lead):
        lead = dict(lead)
        lead["last_checked"] = datetime.now().isoformat(timespec="seconds")
        return self._match(lead, False)
