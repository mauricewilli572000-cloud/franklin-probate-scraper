"""End-to-end run of run_pipeline.main() with the court + Auditor served from fixtures."""
import json
from pathlib import Path

import run_pipeline
from probate import config
from probate.counties import franklin

FX = Path(__file__).parent / "fixtures"


class FakeHttp:
    requests_made = 0

    def __init__(self):
        self.aud = json.loads((FX / "auditor_hunt.json").read_text())

    def get_text(self, url, params=None):
        name = {"PBODateInx": "date_index", "ESTATE_DETAIL": "estate_detail", "PBFidy": "fiduciaries",
                "FID_DETAIL": "fiduciary_detail", "ATTY_DETAIL": "attorney_detail", "PBDocket": "docket"}
        for k, v in name.items():
            if k in url:
                if k == "PBODateInx" and params and "stringf" in params:
                    return "<html></html>"
                return (FX / f"{v}.html").read_text()
        raise AssertionError(url)

    def get_json(self, url, params):
        if "LINDORA" in params["where"] and params["where"].startswith("SITEADDRESS ="):
            return self.aud
        return {"features": []}


def test_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(config, "DOCS_ROOT", tmp_path / "docs")
    monkeypatch.setattr(franklin, "Http", FakeHttp)
    args = ["--county", "franklin", "--start", "2026-11-05", "--end", "2026-11-05"]
    assert run_pipeline.main(args) == 0
    out = tmp_path / "data" / "franklin"
    recs = json.loads((out / "records.json").read_text())
    assert len(recs) == 4                      # every fixture estate resolves to the HUNT parcel
    hunt = next(r for r in recs if r["case_key"] == "648408")
    assert hunt["property"]["parcel_id"] == "010-136230"
    assert hunt["fiduciary"]["street"] == "3647 ROCKY ROAD"
    assert hunt["attorney"]["phone"] == "(614) 289-3056"
    assert hunt["docket_flags"]["real_estate_summary"]
    assert hunt["date_added"] and hunt["county"] == "franklin"
    csv = (out / "ghl_probate_franklin.csv").read_text(encoding="utf-8-sig")
    assert "Jason,Moore" in csv and "5163 LINDORA DR" in csv
    assert "Franklin" in csv
    docs = tmp_path / "docs" / "data" / "franklin"
    assert (docs / "records.json").exists() and json.loads((docs / "meta.json").read_text())["county"] == "franklin"
    # second run keeps date_added and does not duplicate
    assert run_pipeline.main(args) == 0
    assert len(json.loads((out / "records.json").read_text())) == 4
