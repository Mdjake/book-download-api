"""
LibGen Main API - Final Version
Supports 'limit' parameter to control number of results
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
import json
from datetime import datetime, timedelta
import uvicorn
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== FASTAPI APP ==========

app = FastAPI(
    title="LibGen Main API",
    version="8.0.0",
    description="Search LibGen with results limit control"
)

# ========== CORS ==========

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ========== CONFIGURATION ==========

ADBYPASS_API = os.environ.get("ADBYPASS_API", "https://libgen-adbypass.vercel.app")
MAX_WORKERS = 8
TIMEOUT = 4
CACHE_TTL = 7200

# ========== SIMPLE CACHE ==========

class SimpleCache:
    def __init__(self):
        self.data = {}
        self.timestamps = {}
    
    def get(self, key):
        if key in self.data:
            if datetime.now() - self.timestamps[key] < timedelta(seconds=CACHE_TTL):
                return self.data[key]
            else:
                del self.data[key]
                del self.timestamps[key]
        return None
    
    def set(self, key, value):
        self.data[key] = value
        self.timestamps[key] = datetime.now()
    
    def clear(self):
        self.data.clear()
        self.timestamps.clear()

cache = SimpleCache()

# ========== LIBGEN ENGINE ==========

class LibGenEngine:
    def __init__(self):
        self.base_url = "https://libgen.li"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        self.adbypass_api = ADBYPASS_API

    def search(self, query: str, max_pages: int = 1, limit: int = 25, filters: Dict = None) -> Dict:
        """Fast search with results limit"""
        start_time = time.time()
        
        if filters is None:
            filters = {}
        
        # Check cache
        cache_key = f"{query}:{max_pages}:{limit}:{json.dumps(filters, sort_keys=True)}"
        cached = cache.get(cache_key)
        if cached:
            elapsed = time.time() - start_time
            cached['_time_taken'] = f"{elapsed:.2f}s (cached)"
            return cached
        
        results = []
        total_available = 0
        pages_fetched = 0
        
        # Fetch pages in parallel until we have enough results or run out of pages
        with ThreadPoolExecutor(max_workers=max_pages) as executor:
            futures = [
                executor.submit(self._fetch_page, query, page, filters) 
                for page in range(1, max_pages + 1)
            ]
            
            for future in as_completed(futures):
                try:
                    page_results, page_total = future.result(timeout=TIMEOUT)
                    if page_results:
                        results.extend(page_results)
                        if page_total:
                            total_available = page_total
                        pages_fetched += 1
                        
                        # Stop if we have enough results
                        if len(results) >= limit:
                            # Cancel remaining futures
                            for f in futures:
                                if not f.done():
                                    f.cancel()
                            break
                except:
                    pass
        
        # Trim results to limit
        results = results[:limit]
        
        # Format response
        response = {
            'query': query,
            'total_results': len(results),
            'total_available': total_available,
            'pages_fetched': pages_fetched,
            'limit_applied': limit,
            'results': results
        }
        
        cache.set(cache_key, response)
        
        elapsed = time.time() - start_time
        response['_time_taken'] = f"{elapsed:.2f}s"
        
        return response

    def _fetch_page(self, query: str, page: int, filters: Dict) -> tuple:
        """Fetch a single page"""
        try:
            url = self._build_search_url(query, page)
            response = self.session.get(url, timeout=TIMEOUT)
            
            if response.status_code != 200:
                return [], 0
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Get total count
            total_available = 0
            files_tab = soup.find('a', href=re.compile(r'curtab=f'))
            if files_tab:
                badge = files_tab.find('span', class_='badge')
                if badge:
                    try:
                        total_available = int(badge.get_text(strip=True))
                    except:
                        pass
            
            table = soup.find('table', {'id': 'tablelibgen'})
            if not table:
                return [], total_available
            
            rows = table.find_all('tr')[1:]
            page_results = []
            ads_urls = []
            ads_books = []
            
            # Parse rows
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 8:
                    continue
                
                book = self._parse_row(row, cells)
                if book and self._apply_filters(book, filters):
                    if book.get('ads_url'):
                        ads_urls.append(book['ads_url'])
                        ads_books.append(book)
                    else:
                        page_results.append(book)
            
            # Batch bypass
            if ads_urls:
                real_urls = self._batch_bypass(ads_urls)
                for book, real_url in zip(ads_books, real_urls):
                    if real_url:
                        book['download_url'] = real_url
                        book['bypass_success'] = True
                    else:
                        book['bypass_success'] = False
                    book.pop('ads_url', None)
                    page_results.append(book)
            
            return page_results, total_available
            
        except:
            return [], 0

    def _batch_bypass(self, ads_urls: List[str]) -> List[Optional[str]]:
        """Fast batch bypass"""
        if not ads_urls:
            return []
        
        workers = min(len(ads_urls), 3)
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._bypass_single, url): url for url in ads_urls}
            results = []
            for future in as_completed(futures):
                try:
                    results.append(future.result(timeout=3))
                except:
                    results.append(None)
            return results

    def _bypass_single(self, ads_url: str) -> Optional[str]:
        """Single bypass"""
        try:
            resp = self.session.post(
                f"{self.adbypass_api}/bypass",
                json={"url": ads_url},
                timeout=2
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    real = data.get('data', {}).get('real_url')
                    if real:
                        if real.startswith('get.php'):
                            return f"{self.base_url}/{real}"
                        return real
            return None
        except:
            return None

    def _build_search_url(self, query: str, page: int) -> str:
        """Minimal URL builder"""
        query = query.replace(' ', '+')
        return f"{self.base_url}/index.php?req={query}&res=25&filesuns=all&curtab=f&page={page}"

    def _parse_row(self, row, cells) -> Optional[Dict]:
        """Parse row"""
        try:
            bold = cells[0].find('b')
            title = bold.get_text(strip=True) if bold else cells[0].get_text(strip=True)[:80]
            title = re.sub(r'^#\d+\s*', '', title)
            
            md5 = None
            ads_url = None
            download_url = None
            
            for cell in cells:
                for link in cell.find_all('a'):
                    href = link.get('href', '')
                    
                    if 'ads.php?md5=' in href:
                        match = re.search(r'md5=([a-f0-9]{32})', href)
                        if match:
                            md5 = match.group(1)
                            ads_url = f"{self.base_url}{href}"
                            break
                    
                    if 'get.php?md5=' in href and '&key=' in href:
                        match = re.search(r'md5=([a-f0-9]{32})', href)
                        if match:
                            md5 = match.group(1)
                            download_url = f"{self.base_url}{href}"
                            break
                    
                    if 'get.php?md5=' in href:
                        match = re.search(r'md5=([a-f0-9]{32})', href)
                        if match:
                            md5 = match.group(1)
                            ads_url = f"{self.base_url}/ads.php?md5={md5}"
                            break
                
                if md5:
                    break
            
            if not md5:
                text = cells[0].get_text()
                match = re.search(r'md5[=:]\s*([a-f0-9]{32})', text, re.I)
                if match:
                    md5 = match.group(1)
                    ads_url = f"{self.base_url}/ads.php?md5={md5}"
            
            return {
                'title': title[:100],
                'author': cells[1].get_text(strip=True)[:50] if len(cells) > 1 else "",
                'publisher': cells[2].get_text(strip=True)[:50] if len(cells) > 2 else "",
                'year': cells[3].get_text(strip=True) if len(cells) > 3 else "",
                'language': cells[4].get_text(strip=True) if len(cells) > 4 else "",
                'pages': cells[5].get_text(strip=True) if len(cells) > 5 else "",
                'size': cells[6].get_text(strip=True) if len(cells) > 6 else "",
                'extension': cells[7].get_text(strip=True).lower() if len(cells) > 7 else 'unknown',
                'md5': md5,
                'download_url': download_url,
                'bypass_success': False,
                'is_book': bool(row.find('span', class_='badge', string=re.compile(r'b'))),
                'is_comic': bool(row.find('span', class_='badge', string=re.compile(r'c')))
            }
        except:
            return None

    def _apply_filters(self, book: Dict, filters: Dict) -> bool:
        if filters.get('format') and book['extension'] != filters['format']:
            return False
        if filters.get('author') and filters['author'].lower() not in book['author'].lower():
            return False
        if filters.get('language') and book['language'].lower() != filters['language'].lower():
            return False
        return True

    def get_by_md5(self, md5: str) -> Dict:
        """Get download URL by MD5"""
        start_time = time.time()
        
        cached = cache.get(f"md5:{md5}")
        if cached:
            cached['_time_taken'] = f"{time.time() - start_time:.2f}s (cached)"
            return cached
        
        ads_url = f"{self.base_url}/ads.php?md5={md5}"
        
        try:
            resp = self.session.post(
                f"{self.adbypass_api}/bypass",
                json={"url": ads_url},
                timeout=2
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'success':
                    real_url = data.get('data', {}).get('real_url')
                    if real_url:
                        if real_url.startswith('get.php'):
                            real_url = f"{self.base_url}/{real_url}"
                        result = {'success': True, 'md5': md5, 'download_url': real_url}
                        cache.set(f"md5:{md5}", result)
                        result['_time_taken'] = f"{time.time() - start_time:.2f}s"
                        return result
            
            result = {'success': True, 'md5': md5, 'download_url': ads_url}
            cache.set(f"md5:{md5}", result)
            result['_time_taken'] = f"{time.time() - start_time:.2f}s"
            return result
            
        except:
            result = {'success': True, 'md5': md5, 'download_url': ads_url}
            cache.set(f"md5:{md5}", result)
            result['_time_taken'] = f"{time.time() - start_time:.2f}s"
            return result


# ========== INITIALIZE ==========

engine = LibGenEngine()


# ========== API ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "name": "LibGen API",
        "version": "8.0.0",
        "description": "Search LibGen with results limit control",
        "parameters": {
            "query": "Required - Search term",
            "format": "Optional - File format filter (pdf, epub, mobi, etc.)",
            "author": "Optional - Author name filter",
            "language": "Optional - Language filter (en, ru, fr, etc.)",
            "limit": "Optional - Number of results to return (default: 25, max: 50)",
            "max_pages": "Optional - Pages to fetch (default: 1, max: 2)"
        },
        "examples": {
            "search_1_book": "/search?query=think+and+grow+rich&limit=1",
            "search_5_books": "/search?query=psychology&format=pdf&limit=5",
            "search_by_author": "/search?query=python&author=O%27Reilly&limit=10"
        },
        "endpoints": {
            "/search": "Search with results limit",
            "/download/{md5}": "Get download URL by MD5",
            "/formats": "Available formats",
            "/cache/clear": "Clear cache"
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/cache/clear")
async def clear_cache():
    cache.clear()
    return {"status": "success", "message": "Cache cleared"}


@app.get("/search")
async def search(
    query: str = Query(..., description="Search term"),
    format: Optional[str] = Query(None, description="Filter by format (pdf, epub, mobi, etc.)"),
    author: Optional[str] = Query(None, description="Filter by author"),
    language: Optional[str] = Query(None, description="Filter by language (en, ru, fr, etc.)"),
    limit: int = Query(25, ge=1, le=50, description="Number of results to return (max: 50)"),
    max_pages: int = Query(1, ge=1, le=2, description="Pages to fetch (max: 2)")
):
    """
    ⚡ Search LibGen with results limit
    
    Examples:
    - Get 1 book: /search?query=think+and+grow+rich&limit=1
    - Get 5 PDFs: /search?query=psychology&format=pdf&limit=5
    - Get 10 by author: /search?query=python&author=O%27Reilly&limit=10
    """
    if not query:
        raise HTTPException(400, "Query parameter is required")
    
    start_total = time.time()
    
    filters = {
        'format': format,
        'author': author,
        'language': language,
    }
    
    try:
        result = engine.search(query, max_pages, limit, filters)
        total_time = time.time() - start_total
        
        return {
            "status": "success",
            "time_taken": f"{total_time:.2f}s",
            "filters_applied": {k: v for k, v in filters.items() if v},
            "data": result
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/download/{md5}")
async def get_download_url(md5: str):
    """⚡ Get download URL by MD5"""
    start_time = time.time()
    
    if not md5 or len(md5) != 32:
        raise HTTPException(400, "Invalid MD5 hash (must be 32 characters)")
    
    try:
        result = engine.get_by_md5(md5)
        total_time = time.time() - start_time
        
        return {
            "status": "success" if result['success'] else "error",
            "time_taken": f"{total_time:.2f}s",
            "data": result
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/formats")
async def get_formats():
    return {
        "formats": {
            "pdf": "PDF Documents",
            "epub": "EPUB E-books",
            "mobi": "Mobi (Kindle)",
            "azw3": "AZW3 (Kindle)",
            "djvu": "DJVU Scanned",
            "doc": "Word Documents",
            "txt": "Plain Text",
            "fb2": "FictionBook",
            "cbr": "Comic Book (RAR)",
            "cbz": "Comic Book (ZIP)"
        },
        "languages": {
            "en": "English",
            "ru": "Russian",
            "fr": "French",
            "de": "German",
            "es": "Spanish",
            "it": "Italian",
            "pt": "Portuguese",
            "nl": "Dutch",
            "pl": "Polish",
            "uk": "Ukrainian",
            "zh": "Chinese",
            "ja": "Japanese",
            "ar": "Arabic",
            "hi": "Hindi"
        }
    }


# ========== RUN ==========

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)