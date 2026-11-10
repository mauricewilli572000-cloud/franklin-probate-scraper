"""HTTP access to the probate court site and the Auditor parcel service, with retries."""
import logging
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)


class Http:
    def __init__(self, delay=config.REQUEST_DELAY_SEC):
        self.delay = delay
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": config.USER_AGENT,
                               "Accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
        retry = Retry(total=config.MAX_RETRIES, backoff_factor=2,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=("GET",))
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self.s.mount("http://", HTTPAdapter(max_retries=retry))
        self._last = 0.0
        self.requests_made = 0

    def _wait(self):
        gap = time.time() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.time()

    def get_text(self, url, params=None):
        """GET a court page. Params are appended raw: the Net.Data app wants literal ';' in caseno."""
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self._wait()
        self.requests_made += 1
        r = self.s.get(url, timeout=config.TIMEOUT_SEC)
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text

    def get_json(self, url, params):
        self._wait()
        self.requests_made += 1
        r = self.s.get(url, params=params, timeout=config.TIMEOUT_SEC)
        r.raise_for_status()
        return r.json()


class ProbateSite:
    """Thin wrapper over the court's Net.Data endpoints."""

    def __init__(self, http=None):
        self.http = http or Http()

    def date_index(self, yyyymmdd=None, next_key=None):
        if next_key:
            return self.http.get_text(config.DATE_INDEX_URL, {"stringf": next_key})
        return self.http.get_text(config.DATE_INDEX_URL, {"string": yyyymmdd})

    @staticmethod
    def _cn(case_number, suffix=""):
        return f"{case_number};{suffix};"

    def estate_detail(self, case_number, suffix=""):
        return self.http.get_text(config.ESTATE_DETAIL_URL, {"caseno": self._cn(case_number, suffix)})

    def fiduciaries(self, case_number, suffix=""):
        return self.http.get_text(config.FIDUCIARY_LIST_URL, {"caseno": self._cn(case_number, suffix)})

    def fiduciary_detail(self, case_number, fid_no, suffix=""):
        return self.http.get_text(config.FIDUCIARY_DETAIL_URL,
                                  {"caseno": self._cn(case_number, suffix) + fid_no})

    def attorney_detail(self, case_number, fid_no, suffix=""):
        return self.http.get_text(config.ATTORNEY_DETAIL_URL,
                                  {"caseno": self._cn(case_number, suffix) + fid_no})

    def docket(self, case_number, suffix=""):
        return self.http.get_text(config.DOCKET_URL, {"caseno": self._cn(case_number, suffix)})


class BrowserHttp(Http):
    """Same interface as Http, but connects with a Chrome TLS fingerprint (curl_cffi).
    Some county sites (Summit's CourtView) reset connections from Python's default
    TLS client while serving browsers normally."""

    def __init__(self, delay=config.REQUEST_DELAY_SEC):
        super().__init__(delay)
        from curl_cffi import requests as creq
        self.s = creq.Session(impersonate="chrome")
        self.s.headers.update({"Accept-Language": "en-US,en;q=0.9"})

    def _send(self, method, url, **kw):
        last = None
        for attempt in range(config.MAX_RETRIES):
            try:
                r = self.s.request(method, url, timeout=config.TIMEOUT_SEC, **kw)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}")
                r.raise_for_status()
                return r
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("%s %s failed (%s), retry %d", method, url[:80], e, attempt + 1)
                time.sleep(2 * (attempt + 1))
        raise last

    def get_text(self, url, params=None):
        if params:
            url = url + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self._wait()
        self.requests_made += 1
        return self._send("GET", url).text

    def get_json(self, url, params):
        self._wait()
        self.requests_made += 1
        return self._send("GET", url, params=params).json()
