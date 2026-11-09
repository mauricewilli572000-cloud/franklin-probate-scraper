"""Shared lead-building helpers used by every county."""
from .scoring import score

GOOD = ("in_decedent_name", "transferred_to_heirs")


def finish_lead(lead, props, auditor_error=""):
    """Attach parcel matches + score to a county-built lead dict."""
    good = [p for p in props if p["status"] in GOOD]
    lead["property"] = good[0] if good else {}
    lead["other_properties"] = good[1:]
    lead["ruled_out_properties"] = [p for p in props if p not in good]
    lead["auditor_error"] = auditor_error
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
