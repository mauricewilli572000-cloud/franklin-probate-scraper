"""Lead score 0-100 for probate leads (higher = more likely to sell to an investor)."""
from datetime import date


def _days_between(a, b):
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def score(lead):
    s, reasons = 0, []
    prop = lead.get("property") or {}
    sub = (lead.get("subtype") or "").upper()

    if prop.get("status") == "in_decedent_name":
        s += 35; reasons.append("house still in decedent's name")
    elif prop.get("status") == "transferred_to_heirs":
        s += 25; reasons.append("inherited by heirs")

    if "REAL ESTATE ONLY" in sub:
        s += 15; reasons.append("real-estate-only estate")
    elif "FULL ADMINISTRATION" in sub:
        s += 10; reasons.append("full administration")
    elif "SUMMARY" in sub:
        s += 5

    if "W/O WILL" in sub or "WITHOUT WILL" in sub or "NO WILL" in sub:
        s += 10; reasons.append("no will (multiple heirs)")

    fid = lead.get("fiduciary") or {}
    if fid.get("state") and fid["state"].upper() != "OH":
        s += 15; reasons.append("executor out of state")
    elif fid.get("zip") and lead.get("decedent_zip") and fid["zip"][:3] != lead["decedent_zip"][:3]:
        s += 7; reasons.append("executor lives away from property")
    if fid.get("phone"):
        s += 5; reasons.append("executor phone listed")

    if prop:
        if prop.get("has_mortgage_escrow") is False:
            s += 10; reasons.append("no mortgage escrow (possible free & clear)")
        yb = prop.get("year_built")
        if isinstance(yb, int) and yb and yb < 1970:
            s += 5; reasons.append("older home")
        if prop.get("rental"):
            s += 5; reasons.append("registered rental")
        if len(lead.get("other_properties") or []) > 0:
            s += 5; reasons.append("owned multiple parcels")

    flags = lead.get("docket_flags") or {}
    if flags.get("land_sale"):
        s += 10; reasons.append("motion to sell real estate filed")
    elif flags.get("real_estate_summary"):
        s += 5

    d = _days_between(lead.get("date_of_death"), lead.get("filing_date"))
    if d is not None and d > 180:
        s += 5; reasons.append("filed 6+ months after death")

    return min(s, 100), reasons
