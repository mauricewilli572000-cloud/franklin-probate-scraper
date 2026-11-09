"""Settings for the Franklin County, OH probate lead scraper."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
RECORDS_JSON = DATA_DIR / "records.json"          # leads with a confirmed property
PENDING_JSON = DATA_DIR / "pending_records.json"  # estates with no property match yet (retried)
SKIPPED_JSON = DATA_DIR / "skipped_records.json"  # estates checked and ruled out (sold, no property)
GHL_CSV = DATA_DIR / "ghl_probate_leads.csv"

# ---- Probate Court case index (IBM Net.Data app behind the General Case Search page)
PROBATE_BASE = "https://probatesearch.franklincountyohio.gov/netdata"
DATE_INDEX_URL = PROBATE_BASE + "/PBODateInx.ndm/input"            # ?string=YYYYMMDD | ?stringf=<next key>
ESTATE_DETAIL_URL = PROBATE_BASE + "/PBCaseTypeE.ndm/ESTATE_DETAIL"  # ?caseno=648408;;
FIDUCIARY_LIST_URL = PROBATE_BASE + "/PBFidy.ndm/input"             # ?caseno=648408;;
FIDUCIARY_DETAIL_URL = PROBATE_BASE + "/PBFidDetail.ndm/FID_DETAIL"  # ?caseno=648408;;01
ATTORNEY_DETAIL_URL = PROBATE_BASE + "/PBAttyDetail.ndm/ATTY_DETAIL" # ?caseno=648408;;01
DOCKET_URL = PROBATE_BASE + "/PBDocket.ndm/input"                   # ?caseno=648408;;

# ---- Franklin County Auditor live parcel layer (495k parcels, updated weekdays)
AUDITOR_QUERY_URL = ("https://gis.franklincountyohio.gov/hosting/rest/services/"
                     "ParcelFeatures/Parcel_Features/MapServer/0/query")
AUDITOR_FIELDS = ",".join([
    "PARCELID", "SITEADDRESS", "ZIPCD", "OWNERNME1", "OWNERNME2", "MAILNME1", "MAILNME2",
    "PSTLNME1", "PSTLADDRES", "CLASSCD", "CLASSDSCRP", "TOTVALUEBASE", "SALEDATE", "SALEPRICE",
    "RENTAL", "HOMSTD", "OWNEROCCUPIED", "RESYRBLT", "RESFLRAREA_AG", "BEDRMS", "BATHS",
    "ACRES", "CVTTXDSCRP",
])

# ---- Behaviour
CASE_TYPES = {"ESTATE"}          # case "Type" column values to keep
DEFAULT_DAYS_BACK = 10           # nightly run re-scans this many days (court posts with a lag)
PENDING_MAX_DAYS = 60            # keep re-checking unmatched estates this long after filing
REQUEST_DELAY_SEC = 0.4          # be polite to the county servers
TIMEOUT_SEC = 45
MAX_RETRIES = 4
MAX_FIDUCIARIES = 3
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Tax-bill payers that are mortgage escrow servicers -> the house still has a mortgage
MORTGAGE_SERVICERS = (
    "CORELOGIC", "LERETA", "WELLS FARGO", "CHASE", "QUICKEN", "ROCKET", "MR COOPER",
    "NATIONSTAR", "PNC", "HUNTINGTON", "FIFTH THIRD", "US BANK", "U S BANK", "FREEDOM MORTGAGE",
    "LOANCARE", "CENLAR", "PENNYMAC", "NEWREZ", "SHELLPOINT", "SELECT PORTFOLIO", "TAX SERVICE",
    "MORTGAGE", "SERVICING", "BANK", "FINANCIAL", "LENDING", "UNION SAVINGS", "GUARANTEED RATE",
)
