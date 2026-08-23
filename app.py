"""
LibGen Main API - Optimized for Speed
Fast search with real download URLs
"""

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import requests
from bs4 import BeautifulSoup
import re
import json
from datetime import datetime
import uvicorn
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio

# ========== FASTAPI APP ==========

app = FastAPI(
    title="LibGen Main API",
    description="Fast LibGen search with real download URLs",
    version="3.0.0"
)

# ========== CONFIGURATION ==========

ADBYPASS_API = os.environ.get("ADBYPASS_API", "https://libgen-adbypass.vercel.app")
MAX_WORKERS = 5  # Parallel requests
TIMEOUT = 10

# ========== LIBGEN ENGINE ==========

class LibGenEngine:
    def __init__(self):
        self.base_url = "https://libgen.li"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        })
        self.adbypass_api = ADBYPASS_API
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    def search(self, query: str, max_pages: int = 1, filters: Dict = None) -> Dict:
        """Fast search with parallel requests"""
        if filters is None:
            filters = {}
        
        results = []
        total_available = 0
        
        # ✅ Fetch all pages in parallel
        with ThreadPoolExecutor(max_workers=max_pages) as executor:
            futures = {
                executor.submit(self._fetch_page, query, page, filters): page 
                for page in range(1, max_pages + 1)
            }
            
            for future in as_completed(futures):
                page_results, page_total = future.result()
                if page_results:
                    results.extend(page_results)
                    total_available = page_total
        
        return {
            'query': query,
            'total_results': len(results),
            'total_available': total_available,
            'pages_fetched': max_pages,
            'results': results
        }

    def _fetch_page(self, query: str, page: int, filters: Dict) -> tuple:
        """Fetch and parse a single page"""
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
            
            # Parse table
            table = soup.find('table', {'id': 'tablelibgen'})
            if not table:
                return [], total_available
            
            rows = table.find_all('tr')[1:]
            page_results = []
            
            # ✅ Parse rows and collect ads URLs for batch bypass
            books_to_bypass = []
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 8:
                    continue
                
                book = self._parse_row(row, cells)
                if book:
                    # Apply filters
                    if not self._apply_filters(book, filters):
                        continue
                    
                    if book.get('ads_url'):
                        books_to_bypass.append(book)
                    else:
                        page_results.append(book)
            
            # ✅ Batch bypass all ads URLs in parallel
            if books_to_bypass:
                bypass_results = self._batch_bypass([b['ads_url'] for b in books_to_bypass])
                for book, real_url in zip(books_to_bypass, bypass_results):
                    if real_url:
                        book['download_url'] = real_url
                        book['bypass_success'] = True
                    else:
                        book['bypass_success'] = False
                    book.pop('ads_url', None)
                    page_results.append(book)
            
            return page_results, total_available
            
        except Exception:
            return [], 0

    def _batch_bypass(self, ads_urls: List[str]) -> List[Optional[str]]:
        """Bypass multiple ads URLs in parallel"""
        if not ads_urls:
            return []
        
        with ThreadPoolExecutor(max_workers=min(len(ads_urls), MAX_WORKERS)) as executor:
            futures = {executor.submit(self._get_real_url, url): url for url in ads_urls}
            results = []
            for future in as_completed(futures):
                results.append(future.result())
            return results

    def _get_real_url(self, ads_url: str) -> Optional[str]:
        """Get real download URL from AdBypass API"""
        try:
            response = self.session.post(
                f"{self.adbypass_api}/bypass",
                json={"url": ads_url},
                timeout=8
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    real_url = data.get('data', {}).get('real_url')
                    if real_url:
                        if real_url.startswith('get.php'):
                            return f"{self.base_url}/{real_url}"
                        return real_url
            return None
        except Exception:
            return None

    def _build_search_url(self, query: str, page: int) -> str:
        """Build search URL"""
        query = query.replace(' ', '+')
        
        url = f"{self.base_url}/index.php?req={query}"
        
        for col in ['t', 'a', 's', 'y', 'p', 'i']:
            url += f"&columns%5B%5D={col}"
        
        for obj in ['f', 'e', 's', 'a', 'p', 'w']:
            url += f"&objects%5B%5D={obj}"
        
        for topic in ['l', 'c', 'f', 'a', 'm', 'r', 's']:
            url += f"&topics%5B%5D={topic}"
        
        url += f"&res=25&filesuns=all&curtab=f&page={page}"
        
        return url

    def _parse_row(self, row, cells) -> Optional[Dict]:
        """Parse a row"""
        try:
            # Title
            bold_tag = cells[0].find('b')
            if bold_tag:
                title = bold_tag.get_text(strip=True)
                title = re.sub(r'^#\d+\s*', '', title)
            else:
                title = cells[0].get_text(strip=True)[:100]
            
            # Other fields
            author = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            publisher = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            year = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            language = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            pages = cells[5].get_text(strip=True) if len(cells) > 5 else ""
            size = cells[6].get_text(strip=True) if len(cells) > 6 else ""
            extension = cells[7].get_text(strip=True) if len(cells) > 7 else ""
            
            # Extract MD5 and ads URL
            md5 = None
            ads_url = None
            
            for cell in cells:
                links = cell.find_all('a')
                for link in links:
                    href = link.get('href', '')
                    
                    if 'ads.php?md5=' in href or 'get.php?md5=' in href:
                        match = re.search(r'md5=([a-f0-9]{32})', href)
                        if match:
                            md5 = match.group(1)
                            if 'ads.php' in href:
                                ads_url = f"{self.base_url}{href}"
                            break
                
                if md5:
                    break
            
            is_book = bool(row.find('span', class_='badge', string=re.compile(r'b')))
            is_comic = bool(row.find('span', class_='badge', string=re.compile(r'c')))
            
            return {
                'title': title,
                'author': author,
                'publisher': publisher,
                'year': year,
                'language': language,
                'pages': pages,
                'size': size,
                'extension': extension.lower() if extension else 'unknown',
                'md5': md5,
                'ads_url': ads_url,
                'download_url': None,
                'bypass_success': False,
                'is_book': is_book,
                'is_comic': is_comic
            }
            
        except Exception:
            return None

    def _apply_filters(self, book: Dict, filters: Dict) -> bool:
        """Apply filters"""
        if filters.get('format') and filters['format'] != 'all':
            if book['extension'] != filters['format']:
                return False
        
        if filters.get('author'):
            if filters['author'].lower() not in book['author'].lower():
                return False
        
        if filters.get('language') and filters['language'] != 'all':
            if book['language'].lower() != filters['language'].lower():
                return False
        
        if filters.get('year_from'):
            try:
                if book['year'] and int(book['year']) < filters['year_from']:
                    return False
            except:
                pass
        
        if filters.get('year_to'):
            try:
                if book['year'] and int(book['year']) > filters['year_to']:
                    return False
            except:
                pass
        
        return True

    def get_by_md5(self, md5: str) -> Dict:
        """Get real download URL by MD5 hash"""
        ads_url = f"{self.base_url}/ads.php?md5={md5}"
        
        try:
            response = self.session.post(
                f"{self.adbypass_api}/bypass",
                json={"url": ads_url},
                timeout=8
            )
            
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'success':
                    real_url = data.get('data', {}).get('real_url')
                    if real_url:
                        if real_url.startswith('get.php'):
                            real_url = f"{self.base_url}/{real_url}"
                        return {
                            'success': True,
                            'md5': md5,
                            'download_url': real_url
                        }
            
            return {
                'success': False,
                'md5': md5,
                'error': 'Could not get download URL'
            }
            
        except Exception as e:
            return {
                'success': False,
                'md5': md5,
                'error': str(e)
            }


# ========== INITIALIZE ==========

engine = LibGenEngine()


# ========== API ENDPOINTS ==========

@app.get("/")
async def root():
    return {
        "name": "LibGen Main API",
        "version": "3.0.0",
        "description": "Fast LibGen search with real download URLs",
        "endpoints": {
            "/search": "GET - Fast search with filters",
            "/download/{md5}": "GET - Get real download URL by MD5",
            "/formats": "GET - Available formats"
        },
        "example": "/search?query=think+and+grow+rich&format=pdf&max_pages=1"
    }


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/search")
async def search(
    query: str = Query(..., description="Search term"),
    max_pages: int = Query(1, ge=1, le=3, description="Number of pages"),
    format: Optional[str] = Query(None, description="Filter by format"),
    author: Optional[str] = Query(None, description="Filter by author"),
    language: Optional[str] = Query(None, description="Filter by language"),
    year_from: Optional[int] = Query(None, description="Year from"),
    year_to: Optional[int] = Query(None, description="Year to")
):
    """⚡ Fast search with real download URLs"""
    if not query:
        raise HTTPException(status_code=400, detail="Query parameter is required")
    
    filters = {
        'format': format,
        'author': author,
        'language': language,
        'year_from': year_from,
        'year_to': year_to
    }
    
    try:
        result = engine.search(query, max_pages, filters)
        return {
            "status": "success",
            "filters_applied": {k: v for k, v in filters.items() if v is not None},
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/download/{md5}")
async def get_download_url(md5: str):
    """⚡ Get real download URL by MD5"""
    if not md5 or len(md5) != 32:
        raise HTTPException(status_code=400, detail="Invalid MD5 hash")
    
    try:
        result = engine.get_by_md5(md5)
        return {
            "status": "success" if result['success'] else "error",
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            "docx": "Word Documents",
            "txt": "Plain Text",
            "rtf": "Rich Text",
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
            "all": "All Languages"
        }
    }


# ========== RUN ==========

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
