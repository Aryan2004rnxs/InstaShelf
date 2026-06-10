import logging
import urllib.parse
import os
import httpx
import asyncio
import re
from typing import Optional, Dict, Any

logger = logging.getLogger("InstaShelf.enrichment")

# ---------------------------------------------------------------------------
# YouTube URL helpers
# ---------------------------------------------------------------------------

def extract_youtube_video_id(url: str) -> Optional[str]:
    """Extract the 11-character video ID from any YouTube URL format."""
    patterns = [
        r'(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtu\.be/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/v/([a-zA-Z0-9_-]{11})',
        r'(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# YouTube Data API v3 search  (primary — works on HuggingFace)
# ---------------------------------------------------------------------------

async def _search_youtube_api(query: str) -> Optional[Dict[str, Any]]:
    """
    Search YouTube using the official Data API v3.
    Free quota: 100 units per search call, 10,000 units/day.
    That = ~100 search calls/day for free — plenty for a personal bot.

    Returns a dict with keys: video_id, title, channel, url, thumbnail_url
    Returns None if quota is exhausted or API key is missing.
    """
    api_key = os.getenv("YOUTUBE_API_KEY", "")
    if not api_key:
        logger.warning("YOUTUBE_API_KEY not set — skipping API search.")
        return None

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": 1,
        "key": api_key,
    }
    url = "https://www.googleapis.com/youtube/v3/search"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)

        if response.status_code == 403:
            logger.warning("YouTube API quota exhausted (403). Falling back to search URL.")
            return None

        if response.status_code != 200:
            logger.warning(f"YouTube API returned {response.status_code}. Falling back.")
            return None

        data = response.json()
        items = data.get("items", [])
        if not items:
            logger.info(f"YouTube API returned 0 results for: {query}")
            return None

        item = items[0]
        video_id = item["id"]["videoId"]
        snippet = item["snippet"]

        return {
            "video_id": video_id,
            "title": snippet.get("title", query),
            "channel": snippet.get("channelTitle"),
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": (
                snippet.get("thumbnails", {})
                       .get("high", {})
                       .get("url")
                or f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"
            ),
            "duration": None,  # snippet search doesn't return duration
        }

    except Exception as e:
        logger.error(f"YouTube Data API search failed for '{query}': {e}")
        return None


# ---------------------------------------------------------------------------
# Public enrichment functions
# ---------------------------------------------------------------------------

async def enrich_youtube_video(
    title: str,
    direct_url: Optional[str],
    search_query: str,
) -> Dict[str, Any]:
    """
    Resolve a YouTube video to a direct watchable URL.

    Priority:
      1. If a direct YouTube URL was found in the post → extract ID, return immediately.
      2. Search via YouTube Data API v3 → returns real video link.
      3. Fallback → YouTube search results URL so user can find it manually.
    """

    # ── 1. Direct URL already present ──────────────────────────────────────
    if direct_url:
        video_id = extract_youtube_video_id(direct_url)
        if video_id:
            logger.info(f"Direct YouTube URL resolved: {video_id}")
            return {
                "video_id": video_id,
                "title": title,
                "channel": None,
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "thumbnail_url": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
                "duration": None,
            }

    # ── 2. YouTube Data API v3 search ──────────────────────────────────────
    logger.info(f"Searching YouTube API for: {search_query}")
    result = await _search_youtube_api(search_query)

    if result:
        logger.info(f"YouTube API found: {result['title']} ({result['url']})")
        return result

    # ── 2b. Fallback: Relaxed API search ───────────────────────────────────
    # If the exact query failed, remove special characters and try again
    clean_query = re.sub(r'[^\w\s]', ' ', search_query).strip()
    clean_query = re.sub(r'\s+', ' ', clean_query)
    
    if clean_query and clean_query != search_query:
        logger.info(f"Retrying YouTube API with relaxed query: {clean_query}")
        result = await _search_youtube_api(clean_query)
        if result:
            logger.info(f"YouTube API found (relaxed): {result['title']} ({result['url']})")
            return result

    # ── 2c. Fallback: Shorter API search ───────────────────────────────────
    # Keep only the first 3 words and the last 3 words (usually main topic + creator)
    words = clean_query.split()
    if len(words) > 6:
        short_query = " ".join(words[:3] + words[-3:])
        logger.info(f"Retrying YouTube API with short query: {short_query}")
        result = await _search_youtube_api(short_query)
        if result:
            logger.info(f"YouTube API found (short): {result['title']} ({result['url']})")
            return result

    # ── 3. Fallback — search results URL (better than nothing) ─────────────
    encoded_query = urllib.parse.quote(search_query)
    fallback_url = f"https://www.youtube.com/results?search_query={encoded_query}"
    logger.warning(f"All YouTube lookups failed for '{search_query}'. Using search URL fallback.")
    return {
        "video_id": "",
        "title": title,
        "channel": None,
        "url": fallback_url,
        "thumbnail_url": "",
        "duration": None,
    }


async def enrich_book(
    title: str,
    author: Optional[str],
    search_query: str,
) -> Dict[str, Any]:
    """
    Query Open Library API (free, no key needed) for book details.
    Falls back to Open Library search URL if nothing is found.
    """
    encoded_query = urllib.parse.quote(search_query)
    fallback_url = f"https://openlibrary.org/search?q={encoded_query}"
    api_url = f"https://openlibrary.org/search.json?q={encoded_query}&limit=1"

    logger.info(f"Querying Open Library for: {search_query}")

    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            response = await client.get(api_url)

        if response.status_code == 200:
            data = response.json()
            docs = data.get("docs", [])
            if docs:
                doc = docs[0]
                resolved_title = doc.get("title", title)
                authors = doc.get("author_name", [])
                resolved_author = authors[0] if authors else author
                cover_i = doc.get("cover_i")
                work_key = doc.get("key", "")
                publish_year = doc.get("first_publish_year", "")

                thumbnail_url = (
                    f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg"
                    if cover_i else ""
                )
                resolved_url = (
                    f"https://openlibrary.org{work_key}"
                    if work_key else fallback_url
                )

                return {
                    "title": resolved_title,
                    "author": resolved_author,
                    "url": resolved_url,
                    "thumbnail_url": thumbnail_url,
                    "publish_year": publish_year,
                }

    except Exception as e:
        logger.error(f"Open Library lookup failed for '{search_query}': {e}")

    # Fallback
    return {
        "title": title,
        "author": author,
        "url": fallback_url,
        "thumbnail_url": "",
        "publish_year": "",
    }
