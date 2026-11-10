"""Small helpers shared by the county modules."""
import re
from datetime import datetime

from .auditor import DIRECTIONS, SUFFIXES, norm_street

GEN_WORDS = {"JR", "SR", "II", "III", "IV", "V"}
ROLE_PRIORITY = ["EXECUTOR", "ADMINISTRATOR", "FIDUCIARY", "CO-EXECUTOR", "CO-ADMINISTRATOR",
                 "CO-FIDUCIARY", "COMMISSIONER", "APPLICANT", "FIRST APPLICANT", "APPLICANT1",
                 "APPLICANT2", "SECOND APPLICANT", "NEXT OF KIN"]


def clean(s):
    s = (s or "").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s.upper() in {"N/A", "NONE"} else s


def to_last_first(name):
    """'ANTWANE D SMITH' -> 'SMITH, ANTWANE D'; 'RUDOLPH B ELFRINK JR' -> 'ELFRINK, RUDOLPH B, JR'.
    Names that already contain a comma are returned unchanged."""
    name = clean(name).upper()
    if not name or "," in name:
        return name
    parts = name.replace(".", " ").split()
    gen = parts.pop() if len(parts) > 2 and parts[-1] in GEN_WORDS else ""
    if len(parts) < 2:
        return name
    out = f"{parts[-1]}, {' '.join(parts[:-1])}"
    return f"{out}, {gen}" if gen else out


def city_state_zip(text):
    """'CLEVELAND OH 44121-0000' / 'Akron ,   OH   44312' -> ('CLEVELAND', 'OH', '44121')."""
    t = clean(text).upper().replace(" ,", ",")
    m = re.match(r"^(.*?)[,\s]+([A-Z]{2})[,\s]+(\d{5})(?:-\d{4})?$", t)
    if m:
        return m.group(1).strip(" ,"), m.group(2), m.group(3)
    m = re.match(r"^(.*?)[,\s]+([A-Z]{2})$", t)
    return (m.group(1).strip(" ,"), m.group(2), "") if m else (t, "", "")


def mdy(text):
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%A, %B %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(clean(text).title(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def street_core(street):
    """'5163 Lindora Drive' -> ('5163', 'LINDORA'); '1445 E.133RD ST' -> ('1445', '133RD')."""
    s = norm_street(street).replace(".", " ")
    s = re.sub(r"\b([NSEW])(\d)", r"\1 \2", s)            # E.133RD -> E 133RD
    words = s.split()
    if not words or not words[0].isdigit():
        return "", ""
    rest = [w for w in words[1:] if w not in DIRECTIONS.values() and w not in SUFFIXES.values()
            and w not in SUFFIXES]
    return words[0], " ".join(rest)


def same_street(a, b):
    na, ca = street_core(a)
    nb, cb = street_core(b)
    return bool(na) and na == nb and bool(ca) and (ca == cb or ca.startswith(cb) or cb.startswith(ca))


def pick_contact(parties):
    """Best fiduciary/applicant from a list of {'role','name',...} dicts."""
    def rank(p):
        r = p.get("role", "").upper()
        return ROLE_PRIORITY.index(r) if r in ROLE_PRIORITY else len(ROLE_PRIORITY)
    cands = [p for p in parties if rank(p) < len(ROLE_PRIORITY)]
    return sorted(cands, key=rank)[0] if cands else {}
