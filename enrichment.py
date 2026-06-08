import logging
import urllib.parse
import httpx
import asyncio
import re
from typing import Optional, Dict, Any
from youtubesearchpython import VideosSearch

logger = logging.getLogger("InstaShelf.enrichment")

# Extract video ID from common YouTube URL formats
def extract_youtube_video_id(url: str) -> Optional[str]:
    """
    Extracts the 11-character video ID from a YouTube URL.
    """
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None

def _search_youtube_sync(query: str) -> Optional[Dict[str, Any]]:
    """Synchronous YouTube Search logic called inside a thread pool."""
    try:
        search = VideosSearch(query, limit=1)
        res = search.result()
        if res and "result" in res and len(res["result"]) > 0:
            return res["result"][0]
    except Exception as e:
        logger.error(f"Sync YouTube search call failed for '{query}': {e}")
    return None

async def enrich_youtube_video(title: str, direct_url: Optional[str], search_query: str) -> Dict[str, Any]:
    """
    Enriches YouTube video metadata:
    - If direct_url is provided, extract ID and return canonical details.
    - Else, search YouTube by search_query and return top result.
    - If search fails or returns nothing, falls back to a query search page URL.
    """
    if direct_url:
        video_id = extract_youtube_video_id(direct_url)
        if video_id:
            return {
                "video_id": video_id,
                "title": title,
                "channel": None,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                "duration": None
            }
            
    # Search youtube
    logger.info(f"Searching YouTube for query: {search_query}")
    result = await asyncio.to_thread(_search_youtube_sync, search_query)
    
    if result:
        video_id = result.get("id")
        return {
            "video_id": video_id,
            "title": result.get("title", title),
            "channel": result.get("channel", {}).get("name") if result.get("channel") else None,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
            "duration": result.get("duration")
        }
        
    # Fallback if search fails or returns empty
    encoded_query = urllib.parse.quote(search_query)
    logger.warning(f"YouTube search failed for query '{search_query}'. Falling back to search URL.")
    return {
        "video_id": "",
        "title": title,
        "channel": None,
        "url": f"https://www.youtube.com/results?search_query={encoded_query}",
        "thumbnail_url": "",
        "duration": None
    }

async def enrich_book(title: str, author: Optional[str], search_query: str) -> Dict[str, Any]:
    """
    Queries the Open Library API to find book details.
    Returns details including cover thumbnail, resolved title, author, and Open Library link.
    If search fails, returns a fallback search URL.
    """
    encoded_query = urllib.parse.quote(search_query)
    fallback_url = f"https://openlibrary.org/search?q={encoded_query}"
    
    url = f"https://openlibrary.org/search.json?q={encoded_query}&limit=1"
    logger.info(f"Querying Open Library for book: {search_query}")
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=12.0)
            if response.status_code == 200:
                data = response.json()
                docs = data.get("docs", [])
                if docs:
                    doc = docs[0]
                    resolved_title = doc.get("title", title)
                    authors = doc.get("author_name", [])
                    resolved_author = authors[0] if authors else author
                    cover_i = doc.get("cover_i")
                    work_key = doc.get("key") # e.g. "/works/OL12345W"
                    
                    thumbnail_url = ""
                    if cover_i:
                        thumbnail_url = f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg"
                        
                    resolved_url = f"https://openlibrary.org{work_key}" if work_key else fallback_url
                    
                    # Fetch first publish year if available to add to notes
                    publish_year = doc.get("first_publish_year", "")
                    
                    return {
                        "title": resolved_title,
                        "author": resolved_author,
                        "url": resolved_url,
                        "thumbnail_url": thumbnail_url,
                        "publish_year": publish_year
                    }
    except Exception as e:
        logger.error(f"Open Library lookup failed for book '{search_query}': {e}")
        
    # Fallback return
    return {
        "title": title,
        "author": author,
        "url": fallback_url,
        "thumbnail_url": "",
        "publish_year": ""
    }
