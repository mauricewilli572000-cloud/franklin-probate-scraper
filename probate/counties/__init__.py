"""County registry. Each module exposes KEY, LABEL and a County class with
scan(start, end) -> rows  and  enrich(row, skip_auditor) -> lead dict."""
import importlib

COUNTIES = ["franklin"]          # add new county modules here once they are live


def load(key):
    if key not in COUNTIES:
        raise SystemExit(f"unknown county {key!r}; choose from {COUNTIES}")
    return importlib.import_module(f"probate.counties.{key}").County
