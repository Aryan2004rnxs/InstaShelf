import re
from typing import List
from models import GeminiExtractionResponse, ExtractedYouTubeVideo, ExtractedBook, ExtractedLink

# Regex for YouTube URLs
YT_REGEX = r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/v/|youtube\.com/shorts/)([a-zA-Z0-9_-]{11})'

# General URL Regex (excluding instagram and youtube to avoid loops/noise)
URL_REGEX = r'https?://(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)'

# Regex for ISBN numbers (10 or 13 digits)
ISBN_REGEX = r'\b(?:ISBN(?:-10|-13)?\s*)?((?:97[89])?[- ]?(?:\d[- ]?){9,10}\d|(?:\d[- ]?){9}[\dX])\b'

# Regex for book recommendations (e.g. "book: 'Title'" or "Book: Title by Author")
BOOK_PATTERNS = [
    r'(?i)(?:book|read):\s*["\'«]([^"\'»]+)["\'»]\s*(?:by\s+([A-Za-z\s]+))?',
    r'(?i)(?:book|read):\s*([A-Z][a-zA-Z0-9\s:,-]+)\s*(?:by\s+([A-Z][a-zA-Z\s]+))'
]

def clean_isbn(isbn_str: str) -> str:
    """Removes non-alphanumeric chars from ISBN."""
    return re.sub(r'[^0-9X]', '', isbn_str.upper())

def fallback_extract(text: str) -> GeminiExtractionResponse:
    """
    Fallback extractor using regex when Gemini quota is exhausted.
    Extracts YouTube videos, Books, and Links based on regex matching.
    """
    youtube_videos: List[ExtractedYouTubeVideo] = []
    books: List[ExtractedBook] = []
    other_links: List[ExtractedLink] = []
    
    # 1. Extract YouTube Videos
    yt_matches = re.finditer(YT_REGEX, text)
    seen_videos = set()
    for match in yt_matches:
        video_id = match.group(1)
        full_url = match.group(0)
        if video_id not in seen_videos:
            seen_videos.add(video_id)
            
            # Find context line
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            context = text[start:end].replace("\n", " ").strip()
            
            youtube_videos.append(
                ExtractedYouTubeVideo(
                    title=f"YouTube Video {video_id}",
                    channel=None,
                    direct_url=f"https://www.youtube.com/watch?v={video_id}",
                    search_query=video_id,
                    confidence=1.0,
                    context=f"...{context}...",
                    tags=["#tech", "#ai"]
                )
            )
            
    # 2. Extract Books
    # Search for ISBNs
    isbn_matches = re.finditer(ISBN_REGEX, text)
    seen_books = set()
    for match in isbn_matches:
        raw_isbn = match.group(1)
        cleaned_isbn = clean_isbn(raw_isbn)
        if cleaned_isbn not in seen_books:
            seen_books.add(cleaned_isbn)
            
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            context = text[start:end].replace("\n", " ").strip()
            
            books.append(
                ExtractedBook(
                    title=f"Book ISBN {cleaned_isbn}",
                    author=None,
                    search_query=cleaned_isbn,
                    confidence=1.0,
                    context=f"...{context}...",
                    tags=["#books"]
                )
            )
            
    # Search for Book patterns
    for pattern in BOOK_PATTERNS:
        book_matches = re.finditer(pattern, text)
        for match in book_matches:
            title = match.group(1).strip()
            author = match.group(2).strip() if len(match.groups()) > 1 and match.group(2) else None
            
            book_key = f"{title.lower()}:{str(author).lower()}"
            if book_key not in seen_books:
                seen_books.add(book_key)
                
                start = max(0, match.start() - 60)
                end = min(len(text), match.end() + 60)
                context = text[start:end].replace("\n", " ").strip()
                
                books.append(
                    ExtractedBook(
                        title=title,
                        author=author,
                        search_query=f"{title} {author if author else ''}".strip(),
                        confidence=0.8,
                        context=f"...{context}...",
                        tags=["#books"]
                    )
                )

    # 3. Extract General Links
    url_matches = re.finditer(URL_REGEX, text)
    seen_urls = set()
    for match in url_matches:
        url = match.group(0)
        # Filter out youtube and instagram urls
        if "youtube.com" in url or "youtu.be" in url or "instagram.com" in url:
            continue
            
        if url not in seen_urls:
            seen_urls.add(url)
            other_links.append(
                ExtractedLink(
                    url=url,
                    label="Resource Link",
                    tags=["#resources"]
                )
            )
            
    # Compose summary
    summary = "Extracted content using regex fallback (Gemini limit reached)."
    
    return GeminiExtractionResponse(
        youtube_videos=youtube_videos,
        books=books,
        other_links=other_links,
        summary=summary
    )
