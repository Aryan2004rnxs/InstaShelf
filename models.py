from pydantic import BaseModel, Field
from typing import List, Optional

class ExtractedYouTubeVideo(BaseModel):
    title: str = Field(description="Best guess at the video title or description mentioned")
    channel: Optional[str] = Field(default=None, description="Channel name if explicitly mentioned or strongly inferred")
    direct_url: Optional[str] = Field(default=None, description="Full YouTube URL if explicitly provided in the text")
    search_query: str = Field(description="Optimized YouTube search query to find this video (e.g. 'title + channel' or relevant keywords)")
    confidence: float = Field(description="Confidence score (0.0 to 1.0) based on how clearly it is described/referred to")
    context: str = Field(description="The exact sentence or context where this video was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags, e.g. ['#tech', '#ai', '#productivity']")

class ExtractedBook(BaseModel):
    title: str = Field(description="Title of the book")
    author: Optional[str] = Field(default=None, description="Author name if mentioned or known")
    search_query: str = Field(description="Optimized query for Open Library search (e.g. 'title + author')")
    confidence: float = Field(description="Confidence score (0.0 to 1.0)")
    context: str = Field(description="The exact sentence or context where this book was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags, e.g. ['#books', '#finance', '#business']")

class ExtractedLink(BaseModel):
    url: Optional[str] = Field(default=None, description="Full URL of the external link")
    label: Optional[str] = Field(default=None, description="Short description or label of what this link points to")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags, e.g. ['#resources', '#tools']")

class ExtractedAnime(BaseModel):
    title: str = Field(description="Title of the anime")
    search_query: str = Field(description="Optimized query for anime search")
    confidence: float = Field(description="Confidence score (0.0 to 1.0)")
    context: str = Field(description="The exact sentence or context where this anime was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags")

class ExtractedManga(BaseModel):
    title: str = Field(description="Title of the manga or manhwa")
    search_query: str = Field(description="Optimized query for manga search")
    confidence: float = Field(description="Confidence score (0.0 to 1.0)")
    context: str = Field(description="The exact sentence or context where this manga was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags")

class ExtractedMovieTV(BaseModel):
    title: str = Field(description="Title of the movie, drama, or serial")
    type: str = Field(description="Type of content: 'MOVIE' or 'TV_SHOW'")
    search_query: str = Field(description="Optimized query for movie/TV search")
    confidence: float = Field(description="Confidence score (0.0 to 1.0)")
    context: str = Field(description="The exact sentence or context where this movie/TV show was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags")

class ExtractedIdea(BaseModel):
    text: str = Field(description="The thought, quote, or idea extracted")
    author: Optional[str] = Field(default=None, description="Author or speaker if mentioned")
    confidence: float = Field(description="Confidence score (0.0 to 1.0)")
    context: str = Field(description="The exact sentence or context where this idea was mentioned")
    tags: List[str] = Field(default=[], description="1-3 relevant categorizing tags/hashtags")

class GeminiExtractionResponse(BaseModel):
    youtube_videos: List[ExtractedYouTubeVideo] = Field(default=[], description="List of YouTube videos identified in the text")
    books: List[ExtractedBook] = Field(default=[], description="List of books identified in the text")
    other_links: List[ExtractedLink] = Field(default=[], description="List of general external web links identified in the text")
    anime: List[ExtractedAnime] = Field(default=[], description="List of anime identified in the text")
    manga: List[ExtractedManga] = Field(default=[], description="List of manga/manhwa identified in the text")
    movies_tv: List[ExtractedMovieTV] = Field(default=[], description="List of movies, dramas, and serials identified in the text")
    ideas: List[ExtractedIdea] = Field(default=[], description="List of thoughts, quotes, and ideas identified in the text")
    summary: str = Field(description="A concise one-sentence description summarizing the Instagram post's topic or purpose")

class ShelfRow(BaseModel):
    saved_at: str = Field(description="ISO 8601 timestamp when this entry is saved")
    source_type: str = Field(description="Source type: 'REEL' or 'POST'")
    content_type: str = Field(description="Content category: 'YOUTUBE', 'BOOK', 'LINK', 'ANIME', 'MANGA', 'MOVIE_TV', or 'IDEA'")
    title: str = Field(description="Resolved content title")
    creator: Optional[str] = Field(default="", description="Resolved creator or author")
    url: str = Field(description="Final watchable/readable URL")
    thumbnail_url: Optional[str] = Field(default="", description="Thumbnail URL for visual preview")
    confidence: float = Field(description="Gemini's extraction confidence score")
    instagram_url: str = Field(description="Original Instagram URL")
    raw_context: str = Field(description="The sentence or context from which the item was extracted")
    ai_summary: str = Field(description="Gemini's one-sentence post summary")
    content_hash: str = Field(description="MD5 hash for deduplication check")
    status: str = Field(default="UNREAD", description="Read status: 'UNREAD' or 'READ'")
    gemini_notes: Optional[str] = Field(default="", description="Any additional context, tags, or notes from Gemini")
    tags: Optional[str] = Field(default="", description="Space-separated list of hashtags for categorization, e.g., '#tech #ai'")
