"""Summit CourtView parsers + parcel matching, on pages saved from the live site (Nov 2026)."""
import json
from pathlib import Path

from probate.counties import summit as su

FX = Path(__file__).parent / "fixtures" / "summit"
read = lambda n: (FX / n).read_text(encoding="utf-8")  # noqa: E731
URL = "https://search.summitohioprobate.com/eservices/searchresults.page?x=abc"


def test_results():
    cases, nxt = su.parse_results(read("results.html"), URL)
    assert nxt is None                                    # 'Next »' is a plain span on the last page
    full = read("results.html").replace("<span>Next »</span>", '<a href="#">Next »</a>')
    assert su.parse_results(full, URL)[1] == "TRUNCATED"  # AJAX next button -> flagged
    assert list(cases) == ["2026 ES 01048", "2026 ES 01050", "2026 ES 00577 A"]
    c = cases["2026 ES 01048"]
    assert c["file_date"] == "2026-11-02" and c["action"] == "Full Administration W/O Will"
    assert [p["role"] for p in c["parties"]] == ["DECEDENT", "DECEDENT", "APPLICANT"]
    assert c["href"] == "https://search.summitohioprobate.com/eservices/searchresults.page?x=CASE1048"


def test_case_detail():
    c = su.parse_case(read("case.html"))
    assert c["file_date"] == "2026-11-02" and c["action"] == "FULL ADMINISTRATION W/O WILL" and c["status"] == "Open"
    dec, app = c["parties"]
    assert dec == {"role": "DECEDENT", "name": "NELSON, DENNIS JAMES", "dod": "2026-09-01",
                   "street": "2331 E. MARKET STREET, APT. 117", "city": "AKRON", "state": "OH", "zip": "44312",
                   "attorney": ""}
    assert app["role"] == "APPLICANT" and app["street"] == "56 E. DARTMORE AVE" and app["zip"] == "44301"
    assert app["attorney"] == "WESCHLER, PATRICK J."
    assert [d["description"] for d in c["docket"]][:2] == ["Deposit for Estate", "Full Administration"]


class FakeHttp:
    requests_made = 0

    def get_json(self, url, params):
        if "Tax_Parcel_Sales" in url:
            return json.loads(read("sales_dartmore.json"))
        if "DARTMORE" in params["where"] or "NELSON JONATHAN" in params["where"]:
            return json.loads(read("parcels_dartmore.json"))
        return {"features": []}


def test_parcels():
    props, err = su.Parcels(FakeHttp()).match("NELSON, JONATHAN D", "56 E. Dartmore Ave", "44301", "2026-09-01")
    assert err == "" and props[0]["status"] == "in_decedent_name"
    p = props[0]
    assert p["property_address"] == "56 E DARTMORE AVE" and p["market_value"] == 135030
    assert p["property_class"] == "CAPE COD" and p["last_sale_date"] == "2020-06-16" and p["year_built"] == 1941
    assert p["has_mortgage_escrow"] is None and p["property_city"] == "Akron"


def test_csv_falls_back_to_tax_bill_address():
    from probate.export import lead_row
    lead = {"case_name": "WEBB, MAGGIE L.", "case_key": "2026ER00958", "county": "summit", "score": 35,
            "fiduciary": {"name": "PEAKE, DIANN", "title": "APPLICANT", "street": ""},
            "property": {"property_address": "3418 SYMPHONY ST", "owner_mail_street": "3418 SYMPHONY ST",
                         "owner_mail_city": "Akron", "owner_mail_state": "OH", "owner_mail_zip": "44319",
                         "status": "in_decedent_name", "has_mortgage_escrow": None}}
    row = lead_row(lead)
    assert (row["First Name"], row["Last Name"]) == ("Estate of", "Webb, Maggie L.")
    assert (row["Address"], row["City"], row["Postal Code"]) == ("3418 SYMPHONY ST", "Akron", "44319")
    assert row["Contact Person"] == "Peake, Diann" and row["Mailing Source"] == "tax-bill address"
    assert "mail-to-property" in row["Tags"] and row["Mortgage Escrow"] == "Unknown"
