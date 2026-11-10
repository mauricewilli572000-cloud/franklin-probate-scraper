"""Match a decedent to Franklin County Auditor parcels.

Two lookups:
  1. by the decedent's street address on the probate case (most estates list the home), and
  2. by owner name ("LAST FIRST%"), which also finds rentals/land the decedent owned elsewhere.

Each parcel is classified:
  in_decedent_name      owner still the decedent -> prime lead
  transferred_to_heirs  owner changed after death for ~$0 (certificate of transfer) -> heirs own it
  sold                  owner changed after death for real money -> drop
  other_owner           decedent lived there but someone else owns it -> drop
"""
import logging
import re
from datetime import datetime, timezone

from . import config

log = logging.getLogger(__name__)

SUFFIXES = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "DRIVE": "DR", "ROAD": "RD", "COURT": "CT",
    "LANE": "LN", "PLACE": "PL", "BOULEVARD": "BLVD", "CIRCLE": "CIR", "PARKWAY": "PKWY",
    "TERRACE": "TER", "TRAIL": "TRL", "HIGHWAY": "HWY", "SQUARE": "SQ", "LOOP": "LOOP",
    "PIKE": "PIKE", "WAY": "WAY", "COVE": "CV", "CROSSING": "XING", "POINT": "PT",
    "RIDGE": "RDG", "RUN": "RUN", "GLEN": "GLN", "HOLLOW": "HOLW", "MEADOWS": "MDWS",
    "STATION": "STA", "CRESCENT": "CRES", "PASS": "PASS", "PATH": "PATH", "ALLEY": "ALY",
}
DIRECTIONS = {"NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
              "NORTHEAST": "NE", "NORTHWEST": "NW", "SOUTHEAST": "SE", "SOUTHWEST": "SW"}
UNIT_RE = re.compile(r"\s+(?:APT|APARTMENT|UNIT|STE|SUITE|#|BLDG)(?:\b|\d).*$|\s+#.*$")
NAME_JUNK = {"JR", "SR", "II", "III", "IV", "V", "DECEASED", "DEC", "ESTATE", "MR", "MRS", "MS", "DR"}


def norm_street(street):
    s = re.sub(r"[.,]", " ", (street or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    s = UNIT_RE.sub("", s)
    words = s.split(" ")
    words = [DIRECTIONS.get(w, w) for w in words]
    if len(words) > 2:
        words[-1] = SUFFIXES.get(words[-1], words[-1])
    return " ".join(words)


def split_unit(street):
    m = re.search(r"(?:APT|APARTMENT|UNIT|STE|SUITE|#)\s*([A-Z0-9-]+)", (street or "").upper())
    return m.group(1) if m else ""


def name_parts(case_name):
    """'ELFRINK, JR., RUDOLPH BERNARD' -> ('ELFRINK', 'RUDOLPH', 'BERNARD')."""
    raw = (case_name or "").upper()
    if "," in raw:
        last, rest = raw.split(",", 1)
    else:
        bits = raw.split()
        last, rest = (bits[-1], " ".join(bits[:-1])) if bits else ("", "")
    tok = [t for t in re.split(r"[\s.,]+", rest) if t and t not in NAME_JUNK]
    last = re.sub(r"[^A-Z' -]", "", last).strip()
    first = tok[0] if tok else ""
    middle = tok[1] if len(tok) > 1 else ""
    return last, first, middle


def _sql(s):
    return s.replace("'", "''")


PREFIX_RE = re.compile(r"\b(MC|MAC|VAN|VON|DE|DI|DU|ST)\s+(?=[A-Z]{2})")


def _collapse(text):
    """'MC CARDELL ANNE' -> 'MCCARDELL ANNE' (surname prefixes written with a space)."""
    return PREFIX_RE.sub(r"\1", re.sub(r"[^A-Z ]", " ", (text or "").upper()))


def owner_matches(owner_text, last, first):
    words = set(_collapse(owner_text).split())
    last_words = [w for w in _collapse(last).replace(" ", "").split() if w] if PREFIX_RE.search(
        (last or "").upper() + " X") else [w for w in re.sub(r"[^A-Z ]", " ", (last or "").upper()).split() if w]
    if not last_words or not all(w in words for w in last_words):
        return False
    return not first or first in words or (len(first) > 1 and any(w.startswith(first) for w in words))


def owner_segments(owner_text):
    """Split 'STEWART ANNETTE & JEFFREY J.' into ['STEWART ANNETTE', 'STEWART JEFFREY J'] -
    a co-owner listed without a surname shares the first owner's surname."""
    segs = [" ".join(_collapse(x).split()) for x in re.split(r"&|\bAND\b|;", (owner_text or "").upper())]
    segs = [x for x in segs if x]
    if not segs:
        return []
    surname = segs[0].split()[0]
    out = [segs[0]]
    for seg in segs[1:]:
        w = seg.split()
        first_only = len(w) == 1 or (len(w) == 2 and len(w[1]) <= 2)      # 'JEFFREY J', 'MARY'
        out.append(f"{surname} {seg}" if first_only and surname not in w else seg)
    return out


def _epoch_to_iso(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


GEN = {"JR", "SR", "II", "III", "IV"}
OWNER_NOISE = {"TR", "TRUSTEE", "TTEE", "ETAL", "ET", "AL", "LE", "TOD", "SUCC", "CO", "TRS"}


def gen_suffix(case_name):
    """'SMITH, ERIC BRIAN, SR.' -> 'SR'"""
    for t in re.split(r"[\s.,]+", (case_name or "").upper()):
        if t in GEN:
            return t
    return ""


def owner_segment(owner_text, last, first):
    """Tokens of the owner-name segment ('&'-separated) that names LAST + FIRST, else None."""
    for seg in owner_segments(owner_text):
        if owner_matches(seg, last, first):
            return seg.split()
    return None


def same_person(owner_text, last, first, middle="", gen="", strict=False):
    """Owner text names the decedent. strict=True (name-only hits) also needs the middle
    initial to be present on the Auditor side and agree."""
    seg = owner_segment(owner_text, last, first)
    if seg is None:
        return False
    try:
        i = next(k for k, w in enumerate(seg) if w == first or w.startswith(first))
    except StopIteration:
        i = len(seg)
    rest = [w for w in seg[i + 1:] if w not in OWNER_NOISE]
    o_gen = next((w for w in rest if w in GEN), "")
    if gen and o_gen and gen != o_gen:          # decedent SR, house owned by JR
        return False
    if o_gen and not gen and o_gen in {"JR", "III"} and strict:
        return False
    o_mid = next((w for w in rest if w not in GEN), "")
    if middle and o_mid and o_mid[0] != middle[0]:
        return False
    if middle and o_mid and len(middle) > 1 and len(o_mid) > 1 and middle != o_mid:
        return False                            # JEAN vs JO
    if strict and not (middle and o_mid):
        return False
    return True


def loose_same(owner_text, last, first):
    """Address-confirmed nickname match: same last name and first name starting with the
    same two letters (ANET vs ANNETTE, BOB? no - only spelling variants)."""
    if not first or len(first) < 2:
        return False
    last_c = _collapse(last).replace(" ", "") if PREFIX_RE.search((last or "").upper() + " X") else last
    for seg in owner_segments(owner_text):
        words = seg.split()
        if all(w in words for w in re.sub(r"[^A-Z ]", " ", last_c).split()) and \
                any(w != last and len(w) >= 2 and w[:2] == first[:2] for w in words):
            return True
    return False


def classify(parcel, last, first, date_of_death, middle="", gen="", strict=False, ambiguous=False):
    owner = " & ".join(x for x in (parcel.get("OWNERNME1"), parcel.get("OWNERNME2")) if x)
    sale_date = _epoch_to_iso(parcel.get("SALEDATE"))
    price = parcel.get("SALEPRICE") or 0
    if same_person(owner, last, first, middle, gen, strict):
        return "in_decedent_name"
    if ambiguous:           # one of several units at a fuzzy-matched address: can't tell
        return "other_owner"
    if date_of_death and sale_date and sale_date >= date_of_death:
        return "transferred_to_heirs" if price < 5000 else "sold"
    return "other_owner"


def summarize(parcel, status, matched_by):
    payer = (parcel.get("PSTLNME1") or "").upper()
    # None = this county publishes no tax-bill payer, so escrow is unknown
    has_mortgage = any(k in payer for k in config.MORTGAGE_SERVICERS) if "PSTLNME1" in parcel else None
    mail2 = parcel.get("MAILNME2") or ""
    m = re.match(r"(.*?),?\s+([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", mail2.upper())
    return {
        "parcel_id": parcel.get("PARCELID", ""),
        "property_address": parcel.get("SITEADDRESS", "") or "",
        "property_zip": parcel.get("ZIPCD", "") or "",
        "property_city": (parcel.get("CVTTXDSCRP") or "").replace("CITY OF ", "").title(),
        "auditor_owner": " & ".join(x for x in (parcel.get("OWNERNME1"), parcel.get("OWNERNME2")) if x),
        "owner_mail_street": parcel.get("MAILNME1", "") or "",
        "owner_mail_city": m.group(1).strip().title() if m else "",
        "owner_mail_state": m.group(2) if m else "",
        "owner_mail_zip": m.group(3) if m else "",
        "tax_bill_payer": parcel.get("PSTLNME1", "") or "",
        "has_mortgage_escrow": has_mortgage,
        "property_class": parcel.get("CLASSDSCRP", "") or "",
        "class_code": parcel.get("CLASSCD", "") or "",
        "market_value": parcel.get("TOTVALUEBASE") or 0,
        "last_sale_date": _epoch_to_iso(parcel.get("SALEDATE")),
        "last_sale_price": parcel.get("SALEPRICE") or 0,
        "year_built": parcel.get("RESYRBLT") or "",
        "sqft": parcel.get("RESFLRAREA_AG") or "",
        "beds": parcel.get("BEDRMS") or "",
        "baths": parcel.get("BATHS") or "",
        "acres": round(parcel.get("ACRES") or 0, 3),
        "rental": parcel.get("RENTAL") == "Y",
        "status": status,
        "matched_by": matched_by,
        "confidence": "high" if matched_by == "address" else "medium",
    }


class Auditor:
    """Franklin County Auditor matcher. Other counties subclass this and override
    _query / by_address / by_owner, returning rows with the same keys as Franklin
    (PARCELID, SITEADDRESS, ZIPCD, OWNERNME1, OWNERNME2, MAILNME1, MAILNME2, SALEDATE ...)."""

    def __init__(self, http):
        self.http = http

    def _query(self, where, limit=25):
        params = {"where": where, "outFields": config.AUDITOR_FIELDS,
                  "returnGeometry": "false", "resultRecordCount": limit, "f": "json"}
        try:
            j = self.http.get_json(config.AUDITOR_QUERY_URL, params)
        except Exception as e:  # noqa: BLE001 - network issues shouldn't kill the run
            log.warning("auditor query failed (%s): %s", where, e)
            return None
        if "error" in j:
            log.warning("auditor error %s for %s", j["error"], where)
            return None
        return [f["attributes"] for f in j.get("features", [])]

    def by_address(self, street, zip_code=""):
        s = norm_street(street)
        if not re.match(r"^\d+\s+\S", s):
            return [], True
        unit = split_unit(street)
        where = f"SITEADDRESS = '{_sql(s)}'"
        rows = self._query(where)
        if rows is None:
            return None, True
        exact = bool(rows)
        if not rows:  # tolerate suffix differences: '5163 LINDORA%'
            num, *rest = s.split(" ")
            core = " ".join(w for w in rest if w not in SUFFIXES.values() and w not in DIRECTIONS.values())
            if core:
                where = f"SITEADDRESS LIKE '{_sql(num)} %{_sql(core)}%'"
                if zip_code[:5].isdigit():
                    where += f" AND ZIPCD = '{zip_code[:5]}'"
                rows = self._query(where) or []
        if unit and len(rows) > 1:
            rows = [r for r in rows if unit in (r.get("SITEADDRESS") or "")] or rows
        return rows, exact or len(rows) <= 1

    def by_owner(self, last, first):
        if not last or not first:
            return []
        l, f = _sql(last), _sql(first)
        where = (f"(OWNERNME1 LIKE '{l} {f}%' OR OWNERNME2 LIKE '{l} {f}%' "
                 f"OR OWNERNME1 LIKE '{l} {f[0]} %' )")
        return self._query(where) or []

    def match(self, case_name, street, zip_code, date_of_death):
        """Returns (properties, error). properties: list of summarize() dicts, best first."""
        last, first, middle = name_parts(case_name)
        gen = gen_suffix(case_name)
        found = {}
        addr_rows, exact = self.by_address(street, zip_code) if street else ([], True)
        if addr_rows is None:
            return [], "auditor_unavailable"
        for p in addr_rows:
            st = classify(p, last, first, date_of_death, middle, gen, ambiguous=not exact)
            loose = False
            if st in ("other_owner",) and exact and loose_same(
                    " & ".join(x for x in (p.get("OWNERNME1"), p.get("OWNERNME2")) if x), last, first):
                st, loose = "in_decedent_name", True
            found[p["PARCELID"]] = summarize(p, st, "address")
            if loose:
                found[p["PARCELID"]]["confidence"] = "medium"
        # Name-only hits must clearly be the decedent: middle name on both sides and agreeing,
        # plus one of: full middle name match, owner mails to decedent's street, or few candidates.
        cands = [p for p in self.by_owner(last, first) if p["PARCELID"] not in found and
                 classify(p, last, first, "", middle, gen, strict=True, ambiguous=True) == "in_decedent_name"]
        dec_street = norm_street(street)
        for p in cands:
            seg = owner_segment(" & ".join(x for x in (p.get("OWNERNME1"), p.get("OWNERNME2")) if x), last, first) or []
            full_mid = len(middle) > 1 and middle in seg
            mails_home = bool(dec_street) and norm_street(p.get("MAILNME1") or "") == dec_street
            if full_mid or mails_home or len(cands) <= 2:
                found[p["PARCELID"]] = summarize(p, "in_decedent_name", "owner_name")
                found[p["PARCELID"]]["confidence"] = "high" if (full_mid or mails_home) else "medium"
        rank = {"in_decedent_name": 0, "transferred_to_heirs": 1, "sold": 2, "other_owner": 3}
        props = sorted(found.values(), key=lambda x: (rank[x["status"]], x["matched_by"] != "address",
                                                      -(x["market_value"] or 0)))
        return props, ""
