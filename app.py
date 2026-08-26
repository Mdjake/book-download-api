"""
LibGen Main API - Fixed Cache
Never caches empty results
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
from contextlib import contextmanager
import gc

# ========== FASTAPI APP ==========

app = FastAPI(
    title="LibGen Main API",
    version="12.0.0",
    description="Search LibGen with automatic connection management"
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
MAX_WORKERS = 3
TIMEOUT = 4
CACHE_TTL = 7200

# ========== CONNECTION MANAGER ==========

class ConnectionManager:
    def __init__(self):
        self.sessions = []
    
    @contextmanager
    def get_session(self):
        session = None
        try:
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'close',
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=1,
                pool_maxsize=1,
                max_retries=0
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            yield session
        finally:
            if session:
                try:
                    session.close()
                except:
                    pass
            gc.collect()
    
    def close_all(self):
        try:
            gc.collect()
        except:
            pass

# ========== CACHE - FIXED ==========

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
        # ✅ FIX: Don't cache empty results
        if not value:
            return
        
        # ✅ FIX: Don't cache if results are empty
        if isinstance(value, dict):
            results = value.get('results', [])
            if not results or len(results) == 0:
                return
        
        # Limit cache size
        if len(self.data) > 200:
            items = list(self.data.items())
            for k, _ in items[:50]:
                del self.data[k]
                del self.timestamps[k]
        
        self.data[key] = value
        self.timestamps[key] = datetime.now()
    
    def clear(self):
        self.data.clear()
        self.timestamps.clear()
        gc.collect()

cache = SimpleCache()
connection_manager = ConnectionManager()

# ========== RELEVANCE SCORING ==========

class RelevanceScorer:
    @staticmethod
    def calculate_score(book: Dict, query: str) -> float:
        query_words = query.lower().split()
        score = 0.0
        
        title = book.get('title', '').lower()
        author = book.get('author', '').lower()
        publisher = book.get('publisher', '').lower()
        year = book.get('year', '').lower()
        language = book.get('language', '').lower()
        
        # Title matches
        title_score = 0
        for word in query_words:
            if word in title:
                if query.lower() in title:
                    title_score += 100
                else:
                    title_score += 20
        score += title_score * 2.0
        
        # Author matches
        author_score = 0
        for word in query_words:
            if word in author:
                author_score += 15
        score += author_score * 1.5
        
        # Publisher matches
        publisher_score = 0
        for word in query_words:
            if word in publisher:
                publisher_score += 5
        score += publisher_score
        
        # Year matches
        year_score = 0
        for word in query_words:
            if word in year:
                year_score += 2
        score += year_score
        
        # Language matches
        lang_score = 0
        for word in query_words:
            if word in language:
                lang_score += 1
        score += lang_score
        
        # Exact phrase bonus
        if query.lower() in title:
            score += 50
        if query.lower() in author:
            score += 20
        
        return score
    
    @staticmethod
    def rank_results(results: List[Dict], query: str) -> List[Dict]:
        for book in results:
            book['_relevance_score'] = RelevanceScorer.calculate_score(book, query)
        return sorted(results, key=lambda x: x.get('_relevance_score', 0), reverse=True)

# ========== LIBGEN ENGINE ==========

class LibGenEngine:
    def __init__(self):
        self.base_url = "https://libgen.li"
        self.adbypass_api = ADBYPASS_API
        self.scorer = RelevanceScorer()
        self.connection_manager = connection_manager

    def search(self, query: str, max_pages: int = 1, limit: int = 25, filters: Dict = None) -> Dict:
        start_time = time.time()
        
        if filters is None:
            filters = {}
        
        # Cache key
        cache_key = f"{query}:{max_pages}:{limit}:{json.dumps(filters, sort_keys=True)}"
        cached = cache.get(cache_key)
        if cached:
            elapsed = time.time() - start_time
            cached['_time_taken'] = f"{elapsed:.2f}s (cached)"
            return cached
        
        all_results = []
        total_available = 0
        pages_fetched = 0
        
        actual_pages = min(max_pages, 2)
        
        with ThreadPoolExecutor(max_workers=actual_pages) as executor:
            futures = [
                executor.submit(self._fetch_page, query, page, filters) 
                for page in range(1, actual_pages + 1)
            ]
            
            for future in as_completed(futures):
                try:
                    page_results, page_total = future.result(timeout=TIMEOUT)
                    if page_results:
                        all_results.extend(page_results)
                        if page_total:
                            total_available = page_total
                        pages_fetched += 1
                        
                        if len(all_results) >= limit * 2:
                            for f in futures:
                                if not f.done():
                                    f.cancel()
                            break
                except:
                    pass
        
        # ✅ FIX: Only cache if we have results
        if all_results:
            ranked_results = self.scorer.rank_results(all_results, query)
            limited_results = ranked_results[:limit]
            
            response = {
                'query': query,
                'total_results': len(limited_results),
                'total_available': total_available,
                'pages_fetched': pages_fetched,
                'limit_applied': limit,
                'ranking_applied': True,
                'results': limited_results
            }
            
            # ✅ Only cache if we have results
            if limited_results:
                cache.set(cache_key, response)
        else:
            # ✅ Empty response - don't cache
            response = {
                'query': query,
                'total_results': 0,
                'total_available': 0,
                'pages_fetched': 0,
                'limit_applied': limit,
                'ranking_applied': True,
                'results': []
            }
        
        connection_manager.close_all()
        
        elapsed = time.time() - start_time
        response['_time_taken'] = f"{elapsed:.2f}s"
        
        return response

    def _fetch_page(self, query: str, page: int, filters: Dict) -> tuple:
        try:
            with connection_manager.get_session() as session:
                url = self._build_search_url(query, page)
                response = session.get(url, timeout=TIMEOUT)
                
                if response.status_code != 200:
                    return [], 0
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
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
                
                response.close()
                return page_results, total_available
            
        except:
            return [], 0

    def _batch_bypass(self, ads_urls: List[str]) -> List[Optional[str]]:
        if not ads_urls:
            return []
        
        workers = min(len(ads_urls), 2)
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._bypass_single, url): url for url in ads_urls}
            results = []
            for future in as_completed(futures):
                try:
                    results.append(future.result(timeout=3))
                except:
                    results.append(None)
            
            connection_manager.close_all()
            return results

    def _bypass_single(self, ads_url: str) -> Optional[str]:
        try:
            with connection_manager.get_session() as session:
                resp = session.post(
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
                
                resp.close()
                return None
        except:
            return None

    def _build_search_url(self, query: str, page: int) -> str:
        query = query.replace(' ', '+')
        return f"{self.base_url}/index.php?req={query}&res=25&filesuns=all&curtab=f&page={page}"

    def _parse_row(self, row, cells) -> Optional[Dict]:
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
                    
                    if 'get.php?md5=' in href and '&key=' in href:
                        match = re.search(r'md5=([a-f0-9]{32})', href)
                        if match:
                            md5 = match.group(1)
                            download_url = f"{self.base_url}{href}"
                            break
                
                if download_url:
                    break
            
            if not download_url:
                for cell in cells:
                    for link in cell.find_all('a'):
                        href = link.get('href', '')
                        
                        if 'ads.php?md5=' in href:
                            match = re.search(r'md5=([a-f0-9]{32})', href)
                            if match:
                                md5 = match.group(1)
                                ads_url = f"{self.base_url}{href}"
                                break
                    
                    if ads_url:
                        break
            
            if not download_url and not ads_url:
                for cell in cells:
                    for link in cell.find_all('a'):
                        href = link.get('href', '')
                        
                        if 'get.php?md5=' in href:
                            match = re.search(r'md5=([a-f0-9]{32})', href)
                            if match:
                                md5 = match.group(1)
                                ads_url = f"{self.base_url}/ads.php?md5={md5}"
                                break
                    
                    if ads_url:
                        break
            
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
                'ads_url': ads_url,
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
        start_time = time.time()
        
        cached = cache.get(f"md5:{md5}")
        if cached:
            cached['_time_taken'] = f"{time.time() - start_time:.2f}s (cached)"
            return cached
        
        ads_url = f"{self.base_url}/ads.php?md5={md5}"
        
        try:
            with connection_manager.get_session() as session:
                resp = session.post(
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
                
                resp.close()
                
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
        "version": "12.0.0",
        "description": "Search LibGen with automatic connection management",
        "features": {
            "connection_management": "Auto-closes connections after each request",
            "relevance_ranking": "Results ranked by relevance",
            "download_bypass": "Automatic ad bypass",
            "caching": "Results cached for 2 hours (only successful searches)"
        }
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/cache/clear")
async def clear_cache():
    cache.clear()
    connection_manager.close_all()
    return {"status": "success", "message": "Cache cleared"}


@app.get("/search")
async def search(
    query: str = Query(..., description="Search term"),
    format: Optional[str] = Query(None, description="Filter by format"),
    author: Optional[str] = Query(None, description="Filter by author"),
    language: Optional[str] = Query(None, description="Filter by language"),
    limit: int = Query(25, ge=1, le=50, description="Number of results"),
    max_pages: int = Query(1, ge=1, le=2, description="Pages to fetch")
):
    """⚡ Search with automatic connection management"""
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
        
        connection_manager.close_all()
        
        return {
            "status": "success",
            "time_taken": f"{total_time:.2f}s",
            "filters_applied": {k: v for k, v in filters.items() if v},
            "data": result
        }
    except Exception as e:
        connection_manager.close_all()
        raise HTTPException(500, str(e))


@app.get("/download/{md5}")
async def get_download_url(md5: str):
    start_time = time.time()
    
    if not md5 or len(md5) != 32:
        raise HTTPException(400, "Invalid MD5 hash")
    
    try:
        result = engine.get_by_md5(md5)
        total_time = time.time() - start_time
        
        connection_manager.close_all()
        
        return {
            "status": "success" if result['success'] else "error",
            "time_taken": f"{total_time:.2f}s",
            "data": result
        }
    except Exception as e:
        connection_manager.close_all()
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
        }
    }


# ========== RUN ==========

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)