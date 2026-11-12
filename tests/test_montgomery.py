from pathlib import Path

from probate.counties import montgomery as m

FX = Path(__file__).parent / "fixtures" / "montgomery"


def read(name):
    return (FX / name).read_text(encoding="utf-8", errors="replace")


def test_detail():
    d = m.parse_detail(read("detail_2089_0.html"))
    assert d["decedent"] == "GENEVA SCHAFFER" and d["case_number"] == "2026EST02089"
    assert d["case_type"].startswith("TRANSFER OF REAL ESTATE ONLY") and d["status"] == "OPEN"
    assert d["fiduciary"] == {"name": "O'NEAL, SHERRI J", "street": "7783 CILANTRO WAY", "city": "CLAYTON",
                              "state": "OH", "zip": "45315", "phone": ""}
    assert d["attorney"]["name"] == "TREHERNE, JASON E" and d["attorney"]["phone"] == "937-836-3296"
    assert d["docket_url"].startswith(m.BASE + "CASESEARCH_DOCKETx.cfm?")
    d = m.parse_detail(read("detail_1500_0.html"))
    assert d["date_of_death"] == "2026-08-04" and d["status"] == "CLOSED" and d["status_date"] == "2026-09-29"
    assert "W/O WILL" in d["case_type"]


def test_docket():
    e = m.parse_docket(read("docket_1500.html"))
    assert e[0]["date"] == "2026-08-12" and len(e) > 10
    assert any("RELIEVING ESTATE" in x["description"] for x in e)


def row(owner, pid, mail="1 MAIN ST", owner2=""):
    return {"PARCELID": pid, "OWNERNAME1": owner, "OWNERNAME2": owner2, "PARCELLOCATION": "1 MAIN ST",
            "PARCEL LOCATION ZIP": "45405", "PADDR1": mail, "PADDR3": "DAYTON OH 45405 1234", "CLS": "R",
            "LUC": "510", "ASMTTOTAL": "80000", "SALESDTE": "01-JAN-99", "YRBL": "1950", "CITYNAME": "DAYTON"}


def parcels(rows):
    p = m.Parcels(http=None)
    idx = {}
    for r in rows:
        idx.setdefault(r["OWNERNAME1"].split()[0], []).append(r)
    p._by_last = idx
    return p


def test_match_unique_name():
    p = parcels([row("SCHAFFER GENEVA", "A01 1"), row("SCHAFFER JOHN", "A01 2")])
    props, err = p.match("SCHAFFER, GENEVA", "", "", "")
    assert not err and [x["parcel_id"] for x in props] == ["A01 1"]
    assert props[0]["owner_mail_zip"] == "45405" and props[0]["last_sale_date"] == "1999-01-01"


def test_match_two_people_same_name():
    p = parcels([row("SMITH JOHN A", "A01 1"), row("SMITH JOHN B", "A01 2")])
    assert p.match("SMITH, JOHN", "", "", "")[0] == []
    # the fiduciary's address on the tax bill settles it
    props, _ = p.match("SMITH, JOHN", "", "", "", fid_street="1 MAIN ST")
    assert len(props) == 2 and props[0]["confidence"] == "high"
    p = parcels([row("SMITH JOHN A", "A01 1", mail="9 ELM ST"), row("SMITH JOHN B", "A01 2", mail="5 OAK ST")])
    props, _ = p.match("SMITH, JOHN BRIAN", "", "", "")
    assert [x["parcel_id"] for x in props] == ["A01 2"]
