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

async def enrich_anime(title: str, search_query: str) -> Dict[str, Any]:
    encoded_query = urllib.parse.quote(search_query)
    fallback_url = f"https://aniwaves.ru/filter?keyword={encoded_query}"
    api_url = f"https://api.jikan.moe/v4/anime?q={encoded_query}&limit=1"

    logger.info(f"Querying Jikan for Anime: {search_query}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(api_url)

        if response.status_code == 200:
            data = response.json()
            data_list = data.get("data", [])
            if data_list:
                item = data_list[0]
                resolved_title = item.get("title", title)
                url = fallback_url # Use free stream site instead of MAL
                thumbnail_url = item.get("images", {}).get("jpg", {}).get("image_url", "")
                
                return {
                    "title": resolved_title,
                    "url": url,
                    "thumbnail_url": thumbnail_url,
                }
    except Exception as e:
        logger.error(f"Jikan lookup failed for anime '{search_query}': {e}")

    return {
        "title": title,
        "url": fallback_url,
        "thumbnail_url": "",
    }

async def enrich_manga(title: str, search_query: str) -> Dict[str, Any]:
    encoded_query = urllib.parse.quote(search_query)
    fallback_url = f"https://asurascans.com/?s={encoded_query}"
    api_url = f"https://api.jikan.moe/v4/manga?q={encoded_query}&limit=1"

    logger.info(f"Querying Jikan for Manga: {search_query}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(api_url)

        if response.status_code == 200:
            data = response.json()
            data_list = data.get("data", [])
            if data_list:
                item = data_list[0]
                resolved_title = item.get("title", title)
                url = fallback_url # Use AsuraScans link instead of MAL
                thumbnail_url = item.get("images", {}).get("jpg", {}).get("image_url", "")
                
                return {
                    "title": resolved_title,
                    "url": url,
                    "thumbnail_url": thumbnail_url,
                }
    except Exception as e:
        logger.error(f"Jikan lookup failed for manga '{search_query}': {e}")

    return {
        "title": title,
        "url": fallback_url,
        "thumbnail_url": "",
    }

async def get_tmdb_id_from_imdb(imdb_id: str, is_tv: bool = False) -> Optional[str]:
    """Uses Wikidata to map an IMDb ID to a TMDB ID completely keyless."""
    headers = {"User-Agent": "InstaShelfBot/1.0 (hello@instashelf.local)"}
    search_url = f"https://www.wikidata.org/w/api.php?action=query&list=search&srsearch=haswbstatement:P345={imdb_id}&format=json"
    
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
            for attempt in range(3):
                search_res = await client.get(search_url)
                if search_res.status_code == 200:
                    break
                elif search_res.status_code == 429:
                    await asyncio.sleep(1.0 * (attempt + 1))
                else:
                    return None
            
            if search_res.status_code != 200:
                return None

            search_data = search_res.json()
            results = search_data.get("query", {}).get("search", [])
            if not results: return None
            
            qid = results[0]["title"]
            prop = "P4983" if is_tv else "P4947"
            claims_url = f"https://www.wikidata.org/w/api.php?action=wbgetclaims&entity={qid}&property={prop}&format=json"
            
            claims_res = await client.get(claims_url)
            if claims_res.status_code == 200:
                claims_data = claims_res.json()
                claims = claims_data.get("claims", {}).get(prop, [])
                if claims:
                    return claims[0].get("mainsnak", {}).get("datavalue", {}).get("value")
    except Exception as e:
        logger.error(f"Wikidata TMDB resolution failed for {imdb_id}: {e}")
        
    return None

async def enrich_movie_tv(title: str, type_str: str, search_query: str) -> Dict[str, Any]:
    encoded_query = urllib.parse.quote(search_query)
    
    clean_query = re.sub(r'[^a-zA-Z0-9]', '_', search_query.lower())
    first_letter = clean_query[0] if clean_query else 'a'
    api_url = f"https://v3.sg.media-imdb.com/suggestion/{first_letter}/{clean_query}.json"

    logger.info(f"Querying free IMDb Autocomplete for {type_str}: {search_query}")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(api_url)

        if response.status_code == 200:
            data = response.json()
            results = data.get("d", [])
            if results:
                item = results[0]
                resolved_title = item.get("l") or title
                thumbnail_url = item.get("i", {}).get("imageUrl", "")
                imdb_id = item.get("id", "")
                
                media_type = "tv" if type_str == "TV_SHOW" else "movie"
                encoded_title = urllib.parse.quote_plus(resolved_title)
                
                direct_url = f"https://cinema.bz/?s={encoded_query}"
                if imdb_id:
                    is_tv = (type_str == "TV_SHOW")
                    tmdb_id = await get_tmdb_id_from_imdb(imdb_id, is_tv=is_tv)
                    if tmdb_id:
                        direct_url = f"https://cinema.bz/watch?id={tmdb_id}&type={media_type}&title={encoded_title}"
                
                return {
                    "title": resolved_title,
                    "url": direct_url,
                    "thumbnail_url": thumbnail_url,
                }
    except Exception as e:
        logger.error(f"IMDb lookup failed for {type_str} '{search_query}': {e}")

    return {
        "title": title,
        "url": f"https://cinema.bz/?s={encoded_query}",
        "thumbnail_url": "",
    }

async def enrich_idea(text: str, author: Optional[str]) -> Dict[str, Any]:
    # Ideas/Quotes don't have URLs or thumbnails, we just format the title nicely.
    title = f'"{text}"'
    if author:
        title += f" - {author}"
        
    return {
        "title": title,
        "url": "",
        "thumbnail_url": "",
    }
