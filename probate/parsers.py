"""Parsers for the Franklin County Probate Court Net.Data pages.

The HTML is old and malformed (unclosed <a>/<font>), so everything is matched
structurally: label/value <th>/<td> pairs on detail pages, and fixed-width
<td> rows on the list pages.
"""
import re
from datetime import datetime

from bs4 import BeautifulSoup

NA = {"", "N/A", "NA", "NONE", "CASE IS OPEN"}


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def clean(text):
    if text is None:
        return ""
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return "" if text.upper() in NA else text


def to_iso(mdy):
    """'07/26/2026' or '11/05/26' -> '2026-07-26'."""
    mdy = clean(mdy)
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(mdy, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def label_values(html):
    """Detail pages: every <tr><th>Label</th><td>Value</td></tr> -> {label_lower: value}."""
    out = {}
    for th in _soup(html).find_all("th"):
        td = th.find_next_sibling("td")
        if td is None:
            continue
        key = clean(th.get_text()).lower()
        if key and key not in out:
            out[key] = clean(td.get_text())
    return out


# --------------------------------------------------------------------------- date index
CASE_HREF = re.compile(r"caseno=(\d+)([A-Z]{0,2});")


def parse_date_index(html):
    """Returns (rows, next_key, total_records).

    rows: dicts with case_number, suffix, case_name, case_type, subtype, status,
          opened (ISO), closed (ISO), detail_path
    """
    soup = _soup(html)
    rows = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 7:
            continue
        a = tds[0].find("a", href=True)
        if not a:
            continue
        m = CASE_HREF.search(a["href"])
        if not m:
            continue
        rows.append({
            "case_number": m.group(1),
            "suffix": m.group(2),
            "case_name": clean(tds[1].get_text()),
            "case_type": clean(tds[2].get_text()).upper(),
            "subtype": clean(tds[3].get_text()).upper(),
            "status": clean(tds[4].get_text()),
            "opened": to_iso(tds[5].get_text()),
            "closed": to_iso(tds[6].get_text()),
            "detail_path": a["href"],
        })
    next_key = None
    for a in soup.find_all("a", href=True):
        if "Next" in a.get_text():
            m = re.search(r"stringf=([0-9A-Z]+)", a["href"])
            if m:
                next_key = m.group(1)
                break
    tm = re.search(r"Total Records:\s*(\d+)", soup.get_text())
    return rows, next_key, int(tm.group(1)) if tm else len(rows)


# --------------------------------------------------------------------------- estate detail
def parse_estate_detail(html):
    lv = label_values(html)
    return {
        "case_name": lv.get("case name", ""),
        "subtype": lv.get("case subtype", "").upper(),
        "aka": lv.get("aka", ""),
        "decedent_street": lv.get("decedent street", ""),
        "decedent_city": lv.get("city", ""),
        "decedent_state": lv.get("state", ""),
        "decedent_zip": lv.get("zip", ""),
        "date_of_death": to_iso(lv.get("date of death", "")),
        "bond_amount": lv.get("bond amount", ""),
        "date_opened": to_iso(lv.get("date opened", "")),
        "date_closed": to_iso(lv.get("date closed", "")),
        "related_cases": [v for k, v in lv.items() if k.startswith("related case") and v],
    }


# --------------------------------------------------------------------------- fiduciaries
FID_HREF = re.compile(r"FID_DETAIL\?caseno=[^;]*;[^;]*;(\d+)")


def parse_fiduciary_list(html):
    out = []
    for tr in _soup(html).find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 9:
            continue
        a = tds[0].find("a", href=True)
        m = FID_HREF.search(a["href"]) if a else None
        if not m:
            continue
        out.append({
            "fid_no": m.group(1),
            "name": clean(tds[1].get_text()),
            "title_code": clean(tds[2].get_text()),
            "title": clean(tds[3].get_text()).upper(),
            "appointed": to_iso(tds[4].get_text()),
            "terminated": to_iso(tds[5].get_text()),
            "attorney_number": clean(tds[7].get_text()),
            "attorney_name": clean(tds[8].get_text()),
        })
    return out


def parse_fiduciary_detail(html):
    lv = label_values(html)
    return {
        "name": lv.get("estate fiduciaries name", ""),
        "title": lv.get("title description", "").upper(),
        "street": " ".join(x for x in (lv.get("street", ""), lv.get("street2", "")) if x),
        "city": lv.get("city", ""),
        "state": lv.get("state", ""),
        "zip": lv.get("zip", ""),
        "phone": lv.get("phone number", ""),
        "appointed": to_iso(lv.get("appointment date", "")),
        "terminated": to_iso(lv.get("termination date", "")),
    }


def parse_attorney_detail(html):
    lv = label_values(html)
    return {
        "number": lv.get("attorney number", ""),
        "name": lv.get("attorney name", ""),
        "street": " ".join(x for x in (lv.get("street", ""), lv.get("street2", "")) if x),
        "city": lv.get("city", ""),
        "state": lv.get("state", ""),
        "zip": lv.get("zip", ""),
        "phone": lv.get("phone number", ""),
        "email": lv.get("e-mail address", ""),
    }


# --------------------------------------------------------------------------- docket
def parse_docket(html):
    """Docket entries; continuation rows (blank date+code) are folded into the previous entry."""
    entries = []
    for tr in _soup(html).find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != 6:
            continue
        date_, code, desc, ref, receipt, cost = (clean(td.get_text()) for td in tds)
        if date_ and re.match(r"\d\d/\d\d/\d\d", date_):
            entries.append({"date": to_iso(date_), "code": code, "description": desc,
                            "reference": ref, "receipt": receipt, "cost": cost})
        elif entries and (desc or ref) and "DEPOSIT REMAINING" not in desc.upper():
            e = entries[-1]
            if desc:
                e["description"] = (e["description"] + " " + desc).strip()
            if ref:
                e["reference"] = (e["reference"] + " " + ref).strip()
    return entries


def docket_flags(entries):
    text = " | ".join(e["description"].upper() for e in entries)
    codes = {e["code"].lower() for e in entries}
    return {
        "real_estate_summary": "REAL ESTATE SUMMARY" in text or "sum" in codes,
        "certificate_of_transfer": "CERTIFICATE OF TRANSFER" in text,
        "land_sale": bool(re.search(r"SELL REAL|SALE OF REAL|LAND SALE|TO SELL", text)),
        "will_filed": bool(re.search(r"\bWILL\b", text)),
        "inventory_filed": "INVENTORY" in text,
        "last_activity": max((e["date"] for e in entries if e["date"]), default=""),
        "entry_count": len(entries),
    }
