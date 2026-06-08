import hashlib
import re
from typing import Optional

def get_md5(text: str) -> str:
    """Computes MD5 hex digest for a string."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()

def clean_alphanumeric(text: str) -> str:
    """Keeps only lowercase alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", text.lower())

def clean_url(url: str) -> str:
    """Normalizes URL for deduplication."""
    url = url.strip().lower()
    # Remove trailing slash
    if url.endswith("/"):
        url = url[:-1]
    # Remove http:// or https://
    url = re.sub(r"^https?://(?:www\.)?", "", url)
    return url

def compute_youtube_hash(video_id: str, search_query: str) -> str:
    """
    Computes hash for YouTube video: md5(video_id) if video_id exists, 
    otherwise md5(clean(search_query)).
    """
    if video_id and video_id.strip():
        return get_md5(video_id.strip())
    # Fallback to cleaned search query
    return get_md5(clean_alphanumeric(search_query))

def compute_book_hash(isbn: Optional[str], title: str, author: Optional[str]) -> str:
    """
    Computes hash for Book: md5(isbn) if it looks like an ISBN,
    otherwise md5(clean(title + author)).
    """
    if isbn:
        # Check if isbn looks like 10 or 13 digits (optionally with X)
        cleaned_isbn = re.sub(r"[^0-9X]", "", isbn.upper())
        if re.match(r"^\d{9}[\dI|X]$|^\d{13}$", cleaned_isbn):
            return get_md5(cleaned_isbn)
            
    # Fallback to title + author
    author_str = author.strip() if author else ""
    combined = clean_alphanumeric(title) + clean_alphanumeric(author_str)
    return get_md5(combined)

def compute_link_hash(url: str) -> str:
    """Computes hash for generic link: md5(clean(url))."""
    return get_md5(clean_url(url))
