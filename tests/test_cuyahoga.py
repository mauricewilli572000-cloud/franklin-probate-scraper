"""Cuyahoga parsers + parcel matching, on pages saved from the live docket (Nov 2026)."""
import json
from pathlib import Path

from probate.common import city_state_zip, same_street, street_core, to_last_first
from probate.counties import cuyahoga as cu

FX = Path(__file__).parent / "fixtures" / "cuyahoga"
read = lambda n: (FX / n).read_text(encoding="utf-8")  # noqa: E731


def test_results_and_noresults():
    rows, target = cu.parse_results(read("results.html"))
    assert target == "ctl00$mpContentPH$gvSearchResults$ctl02$lbName"
    assert rows[0] == {"name": "ANTWANE D SMITH", "case": "2026EST310685", "address": "1500 S. NOBLE RD.",
                       "role": "DECEDENT"}
    assert rows[1]["role"] == "APPLICANT"
    assert cu.parse_results(read("noresults.html")) == ([], None)


def test_summary_parties_docket():
    s = cu.parse_summary(read("summary.html"))
    assert s["case_full"] == "2026EST310685" and s["case_type"] == "RELEASE NO WILL"
    assert s["filing_date"] == "2026-10-19" and s["status"] == "CLOSED" and s["q"] == "tiJ_BD9zwzyJL9a1l6ckTg"
    p = cu.parse_parties(read("parties.html"))
    assert p[0] == {"role": "DECEDENT", "name": "ANTWANE D SMITH", "dod": "2026-07-23", "street": "1500 S. NOBLE RD.",
                    "city": "CLEVELAND", "state": "OH", "zip": "44121"}
    assert p[1]["role"] == "APPLICANT" and p[1]["street"] == "515 E. 103RD ST." and p[1]["zip"] == "44108"
    assert len(p) == 2                                   # empty right-hand column ignored
    d = cu.parse_docket(read("docket.html"))
    assert len(d) == 4 and d[2]["description"].startswith("DEATH CERTIFICATE")


def test_helpers():
    assert to_last_first("ANTWANE D SMITH") == "SMITH, ANTWANE D"
    assert to_last_first("RUDOLPH B ELFRINK JR") == "ELFRINK, RUDOLPH B, JR"
    assert to_last_first("SMITH, BARBARA") == "SMITH, BARBARA"
    assert city_state_zip("CLEVELAND OH 44121-0000") == ("CLEVELAND", "OH", "44121")
    assert city_state_zip("Akron ,   OH   44312") == ("AKRON", "OH", "44312")
    assert street_core("1445 E.133RD ST") == ("1445", "133")
    assert same_street("5329 W 228 ST", "5329 W 228TH ST") and same_street("3614 E 112 ST", "3614 EAST 112TH ST")
    assert same_street("6283 SOUTH PERKINS RD", "6283 S PERKINS ROAD")
    assert same_street("6859 HIDDEN LAKE TRL", "6859 Hidden Lake Trail")
    assert not same_street("686 EDDY RD", "868 EDDY RD")


class FakeHttp:
    requests_made = 0

    def __init__(self, features):
        self.features = features
        self.queries = []

    def get_json(self, url, params):
        self.queries.append((url.rsplit("/", 2)[-2], params["where"]))
        if "/1/query" in url and "par_addr = '6859'" in params["where"]:
            return {"features": self.features}
        return {"features": []}


def test_parcel_match_nickname():
    feats = json.loads(read("parcels_smith_anet.json"))["features"]
    http = FakeHttp(feats)
    props, err = cu.Parcels(http).match("SMITH, ANNETTE", "6859 HIDDEN LAKE TRAIL", "44141", "2026-06-01")
    assert err == "" and len(props) == 1
    p = props[0]
    assert p["status"] == "in_decedent_name" and p["confidence"] == "medium"     # ANET ~ ANNETTE, by address
    assert p["property_address"] == "6859 HIDDEN LAKE TRL" and p["market_value"] == 325900
    assert p["has_mortgage_escrow"] is None and p["property_city"] == "Brecksville"
    assert {q[0] for q in http.queries} == {"0", "1"}                            # both layers searched


def test_owner_variants_from_live_run():
    from probate.auditor import same_person, owner_segments, loose_same
    assert owner_segments("STEWART  ANNETTE & JEFFREY J. ") == ["STEWART ANNETTE", "STEWART JEFFREY J"]
    assert loose_same("STEWART  ANNETTE & JEFFREY J. ", "STEWART", "JEFFERY")          # Jeffery ~ Jeffrey
    assert same_person("MC CARDELL ANNE", "MCCARDELL", "ANNE")
    assert same_person("KNUPP SHAWN T & COLLINS COURTNEY A", "COLLINS", "COURTNEY", "ANN")
    assert not same_person("HUNTER CAROLYN", "HUNT", "CAROLYN")
