"""
LibGen Main API v13
- Fixes IndentationError in _batch_bypass (the cause of the Vercel error)
- Thread-safe LRU/TTL cache, never caches empty results
- Shared pooled HTTP session, no bare excepts, proper logging
- Sync endpoints (run in threadpool) so the event loop is never blocked
"""

import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("libgen")

# ========== CONFIG ==========

ADBYPASS_API = os.environ.get("ADBYPASS_API", "https://libgen-adbypass.vercel.app")
BASE_URL = os.environ.get("LIBGEN_BASE", "https://libgen.li")
PAGE_TIMEOUT = 5          # seconds per LibGen page request
BYPASS_TIMEOUT = 3        # seconds per bypass request
CACHE_TTL = 7200          # 2 hours
CACHE_MAX = 200
MAX_PAGES = 2
MD5_RE = re.compile(r"md5=([a-fA-F0-9]{32})")

app = FastAPI(
    title="LibGen Main API",
    version="13.0.0",
    description="Search LibGen with relevance ranking and ad-bypass",
    default_response_class=ORJSONResponse,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # "*" origins + credentials is invalid per CORS spec
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== HTTP SESSION (shared, pooled) ==========

def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    retry = Retry(total=1, backoff_factor=0.2, status_forcelist=(502, 503, 504),
                  allowed_methods=("GET", "POST"))
    adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


session = _make_session()

# ========== CACHE (thread-safe LRU + TTL) ==========

class TTLCache:
    def __init__(self, ttl: int, max_size: int):
        self.ttl = ttl
        self.max_size = max_size
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            ts, value = item
            if time.time() - ts > self.ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return deepcopy(value)  # callers can't mutate the cached copy

    def set(self, key: str, value: Any):
        if not value:
            return
        if isinstance(value, dict) and "results" in value and not value["results"]:
            return  # never cache empty searches
        with self._lock:
            self._data[key] = (time.time(), deepcopy(value))
            self._data.move_to_end(key)
            while len(self._data) > self.max_size:
                self._data.popitem(last=False)

    def clear(self):
        with self._lock:
            self._data.clear()


cache = TTLCache(CACHE_TTL, CACHE_MAX)

# ========== RELEVANCE SCORING ==========

class RelevanceScorer:
    @staticmethod
    def score(book: Dict, query: str) -> float:
        q = query.lower().strip()
        words = [w for w in q.split() if w]
        if not words:
            return 0.0

        title = book.get("title", "").lower()
        author = book.get("author", "").lower()
        publisher = book.get("publisher", "").lower()
        year = book.get("year", "").lower()
        language = book.get("language", "").lower()

        score = 0.0
        phrase_in_title = q in title

        for w in words:
            if w in title:
                score += 2.0 * (100 if phrase_in_title else 20)
            if w in author:
                score += 1.5 * 15
            if w in publisher:
                score += 5
            if w in year:
                score += 2
            if w in language:
                score += 1

        if phrase_in_title:
            score += 50
            if title.startswith(q):
                score += 25
        if q in author:
            score += 20

        # small bonus for popular, readable formats
        score += {"pdf": 3, "epub": 3}.get(book.get("extension", ""), 0)
        return score

    @classmethod
    def rank(cls, results: List[Dict], query: str) -> List[Dict]:
        for b in results:
            b["_relevance_score"] = cls.score(b, query)
        return sorted(results, key=lambda b: b["_relevance_score"], reverse=True)


# ========== ENGINE ==========

class LibGenEngine:
    def __init__(self):
        self.base_url = BASE_URL.rstrip("/")
        self.adbypass_api = ADBYPASS_API.rstrip("/")
        self.scorer = RelevanceScorer()

    # ---------- search ----------

    def search(self, query: str, max_pages: int = 1, limit: int = 25,
               filters: Optional[Dict] = None) -> Dict:
        t0 = time.time()
        filters = {k: v for k, v in (filters or {}).items() if v}

        key = f"search:{query.lower().strip()}:{max_pages}:{limit}:{json.dumps(filters, sort_keys=True)}"
        cached = cache.get(key)
        if cached:
            cached["_time_taken"] = f"{time.time() - t0:.2f}s (cached)"
            return cached

        pages = min(max_pages, MAX_PAGES)
        all_results: List[Dict] = []
        total_available = 0
        pages_fetched = 0

        executor = ThreadPoolExecutor(max_workers=pages)
        try:
            futures = [executor.submit(self._fetch_page, query, p, filters)
                       for p in range(1, pages + 1)]
            for f in futures:  # keep page order so output is deterministic
                try:
                    rows, total = f.result(timeout=PAGE_TIMEOUT + BYPASS_TIMEOUT + 2)
                except FutureTimeout:
                    log.warning("page fetch timed out")
                    continue
                except Exception:
                    log.exception("page fetch failed")
                    continue
                if rows:
                    all_results.extend(rows)
                    pages_fetched += 1
                    total_available = total or total_available
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # de-duplicate by md5
        seen, unique = set(), []
        for b in all_results:
            k = b.get("md5") or id(b)
            if k in seen:
                continue
            seen.add(k)
            unique.append(b)

        ranked = self.scorer.rank(unique, query)[:limit]
        response = {
            "query": query,
            "total_results": len(ranked),
            "total_available": total_available,
            "pages_fetched": pages_fetched,
            "limit_applied": limit,
            "ranking_applied": True,
            "results": ranked,
        }
        cache.set(key, response)  # ignored automatically if empty
        response["_time_taken"] = f"{time.time() - t0:.2f}s"
        return response

    def _fetch_page(self, query: str, page: int, filters: Dict) -> Tuple[List[Dict], int]:
        url = self._build_search_url(query, page)
        try:
            resp = session.get(url, timeout=PAGE_TIMEOUT)
            if resp.status_code != 200:
                log.warning("libgen returned %s for page %s", resp.status_code, page)
                return [], 0
            html = resp.text
        except requests.RequestException as e:
            log.warning("libgen request failed: %s", e)
            return [], 0

        soup = BeautifulSoup(html, "html.parser")

        total_available = 0
        files_tab = soup.find("a", href=re.compile(r"curtab=f"))
        if files_tab:
            badge = files_tab.find("span", class_="badge")
            if badge:
                digits = re.sub(r"\D", "", badge.get_text())
                total_available = int(digits) if digits else 0

        table = soup.find("table", {"id": "tablelibgen"})
        if not table:
            return [], total_available

        direct: List[Dict] = []
        needs_bypass: List[Dict] = []

        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 8:
                continue
            book = self._parse_row(row, cells)
            if not book or not self._apply_filters(book, filters):
                continue
            (needs_bypass if book.get("ads_url") else direct).append(book)

        if needs_bypass:
            urls = self._batch_bypass([b["ads_url"] for b in needs_bypass])
            for book, real in zip(needs_bypass, urls):
                if real:
                    book["download_url"] = real
                    book["bypass_success"] = True
                    book.pop("ads_url", None)
                else:
                    # fall back to the ads page so the user still gets a working link
                    book["download_url"] = book["ads_url"]
                    book["bypass_success"] = False
                direct.append(book)

        return direct, total_available

    # ---------- bypass ----------

    def _batch_bypass(self, ads_urls: List[str]) -> List[Optional[str]]:
        """Resolve ads URLs in parallel. Output order == input order."""
        if not ads_urls:
            return []

        executor = ThreadPoolExecutor(max_workers=min(len(ads_urls), 4))
        try:
            futures = [executor.submit(self._bypass_single, u) for u in ads_urls]
            results: List[Optional[str]] = []
            for f in futures:
                try:
                    results.append(f.result(timeout=BYPASS_TIMEOUT + 1))
                except Exception:
                    results.append(None)
            return results
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _bypass_single(self, ads_url: str) -> Optional[str]:
        cached = cache.get(f"bypass:{ads_url}")
        if cached:
            return cached.get("url")
        try:
            resp = session.post(f"{self.adbypass_api}/bypass",
                                json={"url": ads_url}, timeout=BYPASS_TIMEOUT)
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("status") != "success":
                return None
            real = (data.get("data") or {}).get("real_url")
            if not real:
                return None
            if real.startswith("get.php"):
                real = f"{self.base_url}/{real}"
            cache.set(f"bypass:{ads_url}", {"url": real})
            return real
        except (requests.RequestException, ValueError) as e:
            log.info("bypass failed for %s: %s", ads_url, e)
            return None

    # ---------- parsing ----------

    def _build_search_url(self, query: str, page: int) -> str:
        return (f"{self.base_url}/index.php?req={quote_plus(query)}"
                f"&res=25&filesuns=all&curtab=f&page={page}")

    @staticmethod
    def _find_link(cells, needle: str, require: Optional[str] = None):
        for cell in cells:
            for a in cell.find_all("a", href=True):
                href = a["href"]
                if needle in href and (require is None or require in href):
                    m = MD5_RE.search(href)
                    if m:
                        return m.group(1).lower(), href
        return None, None

    def _parse_row(self, row, cells) -> Optional[Dict]:
        try:
            bold = cells[0].find("b")
            title = bold.get_text(strip=True) if bold else cells[0].get_text(strip=True)[:80]
            title = re.sub(r"^#\d+\s*", "", title)

            md5 = ads_url = download_url = None

            # 1) direct link with key
            md5, href = self._find_link(cells, "get.php?md5=", "&key=")
            if md5:
                download_url = urljoin(self.base_url + "/", href)
            else:
                # 2) ads page
                md5, href = self._find_link(cells, "ads.php?md5=")
                if md5:
                    ads_url = urljoin(self.base_url + "/", href)
                else:
                    # 3) get.php without key -> build ads url
                    md5, _ = self._find_link(cells, "get.php?md5=")
                    if md5:
                        ads_url = f"{self.base_url}/ads.php?md5={md5}"

            def txt(i: int, n: Optional[int] = None) -> str:
                if i >= len(cells):
                    return ""
                s = cells[i].get_text(strip=True)
                return s[:n] if n else s

            return {
                "title": title[:100],
                "author": txt(1, 50),
                "publisher": txt(2, 50),
                "year": txt(3),
                "language": txt(4),
                "pages": txt(5),
                "size": txt(6),
                "extension": txt(7).lower() or "unknown",
                "md5": md5,
                "ads_url": ads_url,
                "download_url": download_url,
                "bypass_success": bool(download_url),
                "is_book": bool(row.find("span", class_="badge", string=re.compile(r"b"))),
                "is_comic": bool(row.find("span", class_="badge", string=re.compile(r"c"))),
            }
        except Exception:
            log.exception("row parse failed")
            return None

    @staticmethod
    def _apply_filters(book: Dict, filters: Dict) -> bool:
        fmt = filters.get("format")
        if fmt and book["extension"] != fmt.lower().lstrip("."):
            return False
        author = filters.get("author")
        if author and author.lower() not in book["author"].lower():
            return False
        lang = filters.get("language")
        if lang and book["language"].lower() != lang.lower():
            return False
        return True

    # ---------- single md5 ----------

    def get_by_md5(self, md5: str) -> Dict:
        t0 = time.time()
        md5 = md5.lower()
        cached = cache.get(f"md5:{md5}")
        if cached:
            cached["_time_taken"] = f"{time.time() - t0:.2f}s (cached)"
            return cached

        ads_url = f"{self.base_url}/ads.php?md5={md5}"
        real = self._bypass_single(ads_url)
        result = {
            "success": True,
            "md5": md5,
            "download_url": real or ads_url,
            "bypass_success": bool(real),
        }
        if real:  # only cache real, resolved links
            cache.set(f"md5:{md5}", result)
        result["_time_taken"] = f"{time.time() - t0:.2f}s"
        return result


engine = LibGenEngine()

# ========== ENDPOINTS (sync -> run in threadpool, don't block the event loop) ==========

@app.get("/")
def root():
    return {
        "name": "LibGen API",
        "version": app.version,
        "endpoints": ["/search", "/download/{md5}", "/formats", "/health", "/docs"],
        "features": {
            "relevance_ranking": "Results ranked by relevance",
            "download_bypass": "Automatic ad bypass with fallback link",
            "caching": "2h LRU cache, successful results only",
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/cache/clear")
def clear_cache():
    cache.clear()
    return {"status": "success", "message": "Cache cleared"}


@app.get("/search")
def search(
    query: str = Query(..., min_length=1, max_length=200, description="Search term"),
    format: Optional[str] = Query(None, description="Filter by format, e.g. pdf"),
    author: Optional[str] = Query(None, description="Filter by author"),
    language: Optional[str] = Query(None, description="Filter by language"),
    limit: int = Query(25, ge=1, le=50),
    max_pages: int = Query(1, ge=1, le=MAX_PAGES),
):
    query = query.strip()
    if not query:
        raise HTTPException(400, "Query parameter is required")

    t0 = time.time()
    filters = {"format": format, "author": author, "language": language}
    try:
        data = engine.search(query, max_pages, limit, filters)
    except Exception as e:
        log.exception("search failed")
        raise HTTPException(500, f"Search failed: {e}")

    return {
        "status": "success",
        "time_taken": f"{time.time() - t0:.2f}s",
        "filters_applied": {k: v for k, v in filters.items() if v},
        "data": data,
    }


@app.get("/download/{md5}")
def get_download_url(md5: str):
    if not re.fullmatch(r"[a-fA-F0-9]{32}", md5):
        raise HTTPException(400, "Invalid MD5 hash")
    t0 = time.time()
    try:
        data = engine.get_by_md5(md5)
    except Exception as e:
        log.exception("download lookup failed")
        raise HTTPException(500, str(e))
    return {
        "status": "success" if data["success"] else "error",
        "time_taken": f"{time.time() - t0:.2f}s",
        "data": data,
    }


@app.get("/formats")
def get_formats():
    return {
        "formats": {
            "pdf": "PDF Documents", "epub": "EPUB E-books", "mobi": "Mobi (Kindle)",
            "azw3": "AZW3 (Kindle)", "djvu": "DJVU Scanned", "doc": "Word Documents",
            "txt": "Plain Text", "fb2": "FictionBook",
            "cbr": "Comic Book (RAR)", "cbz": "Comic Book (ZIP)",
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
