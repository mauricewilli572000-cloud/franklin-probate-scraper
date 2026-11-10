"""GoHighLevel CSV export. Contact = the estate's fiduciary (executor/applicant)."""
import csv

COLUMNS = [
    "First Name", "Last Name", "Phone", "Address", "City", "State", "Postal Code",
    "Property Address", "Property City", "Property Zip", "Parcel ID", "Market Value",
    "Mortgage Escrow", "Property Class", "Property Status",
    "Decedent", "Date of Death", "Case Number", "Case Subtype", "Filing Date",
    "Attorney", "Attorney Phone", "County", "Lead Score", "Score Reasons", "Date Added", "Tags",
]


def _split_name(name):
    """'MOORE, JASON G' -> ('Jason', 'Moore'). Companies/banks go entirely into Last Name."""
    name = (name or "").strip()
    if "," in name:
        last, first = name.split(",", 1)
        first = first.strip().split(" ")[0]
        return first.title(), last.strip().title()
    return "", name.title()


def lead_row(lead):
    fid = lead.get("fiduciary") or {}
    prop = lead.get("property") or {}
    att = lead.get("attorney") or {}
    first, last = _split_name(fid.get("name"))
    tags = ["probate", f"{lead.get('county') or 'franklin'}-county"]
    if prop.get("status") == "transferred_to_heirs":
        tags.append("inherited")
    if fid.get("state") and fid["state"] != "OH":
        tags.append("out-of-state-executor")
    if prop.get("has_mortgage_escrow") is False:
        tags.append("possible-free-clear")
    return {
        "First Name": first, "Last Name": last, "Phone": fid.get("phone", ""),
        "Address": fid.get("street", ""), "City": (fid.get("city") or "").title(),
        "State": fid.get("state", ""), "Postal Code": fid.get("zip", ""),
        "Property Address": prop.get("property_address", ""),
        "Property City": prop.get("property_city", ""), "Property Zip": prop.get("property_zip", ""),
        "Parcel ID": prop.get("parcel_id", ""), "Market Value": prop.get("market_value", ""),
        "Mortgage Escrow": {True: "Yes", False: "No"}.get(prop.get("has_mortgage_escrow"), "Unknown"),
        "Property Class": prop.get("property_class", ""), "Property Status": prop.get("status", ""),
        "Decedent": lead.get("case_name", ""), "Date of Death": lead.get("date_of_death", ""),
        "Case Number": lead.get("case_key", ""), "Case Subtype": lead.get("subtype", ""),
        "Filing Date": lead.get("filing_date", ""),
        "Attorney": att.get("name", ""), "Attorney Phone": att.get("phone", ""),
        "County": (lead.get("county") or "").title(), "Lead Score": lead.get("score", 0), "Score Reasons": "; ".join(lead.get("score_reasons", [])),
        "Date Added": lead.get("date_added", ""), "Tags": ",".join(tags),
    }


def export_ghl_csv(leads, path):
    rows = [lead_row(l) for l in leads if (l.get("fiduciary") or {}).get("street")]
    rows.sort(key=lambda r: (-int(r["Lead Score"] or 0), r["Filing Date"]))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
