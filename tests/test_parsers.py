import json
from pathlib import Path

from probate import parsers
from probate.auditor import classify, name_parts, norm_street, summarize, owner_matches, same_person, gen_suffix
from probate.export import lead_row
from probate.scoring import score

FX = Path(__file__).parent / "fixtures"


def read(name):
    return (FX / name).read_text(encoding="utf-8")


def test_date_index():
    rows, nxt, total = parsers.parse_date_index(read("date_index.html"))
    assert total == 40
    assert nxt == "20261105648437"
    assert len(rows) == 8
    hunt = next(r for r in rows if r["case_number"] == "648408")
    assert hunt == {**hunt, "case_name": "HUNT, CAROLYN S", "case_type": "ESTATE",
                    "subtype": "FULL ADMINISTRATION WITHOUT WILL", "status": "00",
                    "opened": "2026-11-05", "closed": "", "suffix": ""}
    civil = rows[0]
    assert civil["case_number"] == "638005" and civil["suffix"] == "A"
    assert sum(r["case_type"] == "ESTATE" for r in rows) == 4


def test_estate_detail():
    d = parsers.parse_estate_detail(read("estate_detail.html"))
    assert d["decedent_street"] == "5163 LINDORA DRIVE"
    assert (d["decedent_city"], d["decedent_state"], d["decedent_zip"]) == ("COLUMBUS", "OH", "43232")
    assert d["date_of_death"] == "2026-07-26"
    assert d["date_opened"] == "2026-11-05"
    assert d["date_closed"] == ""
    assert d["aka"] == ""


def test_fiduciaries():
    f = parsers.parse_fiduciary_list(read("fiduciaries.html"))
    assert f == [{"fid_no": "01", "name": "MOORE, JASON G", "title_code": "12", "title": "APPLICANT",
                  "appointed": "", "terminated": "", "attorney_number": "0095301",
                  "attorney_name": "ORR, CHRISTOPHER R"}]
    d = parsers.parse_fiduciary_detail(read("fiduciary_detail.html"))
    assert d["street"] == "3647 ROCKY ROAD" and d["zip"] == "43223" and d["phone"] == ""
    assert d["name"] == "MOORE, JASON G"
    a = parsers.parse_attorney_detail(read("attorney_detail.html"))
    assert a["phone"] == "(614) 289-3056" and a["city"] == "NEW ALBANY"


def test_docket():
    e = parsers.parse_docket(read("docket.html"))
    descs = [x["description"] for x in e]
    assert "Franklin County Auditor's Real Estate Summary" in descs
    assert "Application for Authority to Administer Estate" in descs
    assert "Death Certificate Carolyn Sue Hunt" in descs
    assert not any("DEPOSIT REMAINING" in d for d in descs)
    fl = parsers.docket_flags(e)
    assert fl["real_estate_summary"] and not fl["land_sale"]


def test_names_and_addresses():
    assert name_parts("HUNT, CAROLYN S") == ("HUNT", "CAROLYN", "S")
    assert name_parts("ELFRINK, JR., RUDOLPH BERNARD") == ("ELFRINK", "RUDOLPH", "BERNARD")
    assert name_parts("BAKER-HARNER, SANDRA L") == ("BAKER-HARNER", "SANDRA", "L")
    assert norm_street("5163 Lindora Drive") == "5163 LINDORA DR"
    assert norm_street("123 North High Street Apt 4") == "123 N HIGH ST"
    assert owner_matches("HUNT CAROLYN SUE", "HUNT", "CAROLYN")
    assert not owner_matches("HUNTER CAROLYN", "HUNT", "CAROLYN")
    assert gen_suffix("SMITH, ERIC BRIAN, SR.") == "SR"
    # live cases from Nov 2026 filings
    assert not same_person("SMITH CRAIG S & SMITH BARBARA K", "SMITH", "BARBARA", "JEAN", strict=True)
    assert not same_person("IRWIN ALICE A & IRWIN JAMES R", "IRWIN", "JAMES", "DOUGLAS", strict=True)
    assert not same_person("MYERS FRANK & MYERS JAYNE", "MYERS", "FRANK", "H", strict=True)
    assert same_person("MYERS FRANK & MYERS JAYNE", "MYERS", "FRANK", "H")          # at decedent's address: ok
    assert not same_person("SMITH BARBARA JO", "SMITH", "BARBARA", "JEAN", strict=True)
    assert same_person("LOWERY DIANNE L", "LOWERY", "DIANNE", "L", strict=True)
    assert not same_person("SMITH ERIC B JR", "SMITH", "ERIC", "BRIAN", "SR")
    assert same_person("KNUPP SHAWN T & COLLINS COURTNEY A", "COLLINS", "COURTNEY", "ANN")
    assert same_person("WICKNER MARILYN TR", "WICKNER", "MARILYN", "A")
    assert same_person("MELRAGON PATRICK E TTEE", "MELRAGON", "PATRICK", "E")


def test_auditor_classify_and_score():
    p = json.loads(read("auditor_hunt.json"))["features"][0]["attributes"]
    assert classify(p, "HUNT", "CAROLYN", "2026-07-26") == "in_decedent_name"
    heirs = {**p, "OWNERNME1": "MOORE JASON G", "SALEDATE": 1793000000000, "SALEPRICE": 0}
    assert classify(heirs, "HUNT", "CAROLYN", "2026-07-26") == "transferred_to_heirs"
    sold = {**heirs, "SALEPRICE": 210000}
    assert classify(sold, "HUNT", "CAROLYN", "2026-07-26") == "sold"
    s = summarize(p, "in_decedent_name", "address")
    assert s["has_mortgage_escrow"] and s["market_value"] == 176900 and s["last_sale_date"] == "2017-04-05"
    assert s["owner_mail_city"] == "Columbus" and s["owner_mail_zip"] == "43232"
    lead = {"subtype": "FULL ADMINISTRATION WITHOUT WILL", "property": s, "decedent_zip": "43232",
            "fiduciary": {"name": "MOORE, JASON G", "street": "3647 ROCKY ROAD", "city": "COLUMBUS",
                          "state": "OH", "zip": "43223", "phone": ""},
            "date_of_death": "2026-07-26", "filing_date": "2026-11-05", "docket_flags": {}}
    sc, why = score(lead)
    assert sc == 35 + 10 + 10 + 5   # in name, full admin, no will, older home
    row = lead_row({**lead, "case_key": "648408", "case_name": "HUNT, CAROLYN S", "score": sc})
    assert row["First Name"] == "Jason" and row["Last Name"] == "Moore"
    assert row["Property Address"] == "5163 LINDORA DR" and row["Mortgage Escrow"] == "Yes"
