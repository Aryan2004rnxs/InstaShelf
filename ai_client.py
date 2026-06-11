import os
import json
import logging
import asyncio
import base64
import mimetypes
from typing import List, Optional, Dict, Any, Set
from PIL import Image
from google import genai
from google.genai import types
import httpx

from models import GeminiExtractionResponse
from utils import (
    increment_gemini_usage,
    get_gemini_usage,
    increment_groq_usage,
    get_groq_usage
)
from fallback_extractor import fallback_extract

logger = logging.getLogger("InstaShelf.ai_client")

# Initialize Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
gemini_client = None
if GEMINI_KEY:
    gemini_client = genai.Client(api_key=GEMINI_KEY)
else:
    logger.warning("GEMINI_API_KEY is not set in environment variables.")

# Initialize Groq configuration
GROQ_KEY = os.getenv("GROQ_API_KEY")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TEXT_MODEL = os.getenv("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")

# Gemini Model configuration
GEMINI_MODEL_NAME = "gemini-2.5-flash"
SYSTEM_PROMPT = (
    "You are a content extraction assistant. Your job is to analyze text or images from "
    "Instagram posts and identify all references to YouTube videos, books, podcasts, courses, "
    "anime, manga, movies, TV shows, quotes, ideas, and useful links."
)

# Global OCR Reader lazy singleton
_ocr_reader = None

def get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        logger.info("Initializing EasyOCR reader (CPU mode)...")
        _ocr_reader = easyocr.Reader(['en'], gpu=False)
    return _ocr_reader

def clean_json_text(text: str) -> str:
    """Cleans up markdown code blocks if the model outputs them despite instructions."""
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()

def image_to_base64(image_path: str) -> str:
    """Reads an image file and returns its base64-encoded string representation."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

async def call_groq_with_quota(
    messages: List[Dict[str, Any]],
    model_name: str,
    json_mode: bool = False
) -> str:
    """
    Wrapper for Groq API that checks and tracks the 1000 daily quota.
    Raises RuntimeError if quota is exceeded, Groq key is missing, or request fails.
    """
    if not GROQ_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
        
    usage = get_groq_usage()
    if usage >= 1000:
        logger.warning(f"Groq API quota exceeded for today ({usage}/1000).")
        raise RuntimeError("Groq Quota Limit Exceeded")
        
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": 0.0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        
    async with httpx.AsyncClient(timeout=90.0) as client:
        response = await client.post(GROQ_URL, headers=headers, json=payload)
        
    if response.status_code != 200:
        logger.error(f"Groq API returned error {response.status_code}: {response.text}")
        raise RuntimeError(f"Groq API error ({response.status_code}): {response.text}")
        
    # Increment usage count in SQLite
    increment_groq_usage()
    
    data = response.json()
    try:
        result_text = data["choices"][0]["message"]["content"]
        return result_text
    except (KeyError, IndexError) as err:
        logger.error(f"Unexpected response structure from Groq: {data}")
        raise RuntimeError("Invalid response structure from Groq API")

async def call_gemini_with_quota(model_name: str, contents: List[Any], system_instruction: str = None, json_mode: bool = False, temperature: float = None) -> str:
    """
    Wrapper for model.generate_content that checks and tracks the 20 daily quota.
    Raises RuntimeError if quota is exceeded.
    """
    if not gemini_client:
        raise RuntimeError("GEMINI_API_KEY is not set.")
        
    usage = get_gemini_usage()
    if usage >= 20:
        logger.warning(f"Gemini API quota exceeded for today ({usage}/20).")
        raise RuntimeError("Gemini Quota Limit Exceeded")
        
    increment_gemini_usage()
    
    config_kwargs = {}
    if system_instruction:
        config_kwargs["system_instruction"] = system_instruction
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    if temperature is not None:
        config_kwargs["temperature"] = temperature
        
    # The new SDK takes a GenerateContentConfig object
    config = types.GenerateContentConfig(**config_kwargs) if config_kwargs else None
    
    loop = asyncio.get_running_loop()
    response = await loop.run_in_executor(
        None,
        lambda: gemini_client.models.generate_content(
            model=model_name,
            contents=contents,
            config=config
        )
    )
        
    return response.text

async def extract_content_with_ai(raw_text: str, image_paths: List[str] = None) -> GeminiExtractionResponse:
    """
    Main entry point for AI extraction.
    For POST:
      Attempts Groq multimodal vision extraction first.
      If it fails/quota is hit, attempts Gemini multimodal vision extraction.
      If both fail, runs easyOCR on images and runs text-only extraction (Groq first, then Gemini).
    For REEL:
      Runs text-only extraction on caption + subtitles (Groq first, then Gemini).
      
    Falls back to regex extraction (fallback_extractor.py) if all AI methods fail or quotas are hit.
    """
    prompt_template = """Analyze this text or images from an Instagram post and extract ALL references to external content (YouTube videos, books, courses, anime, manga, movies, tv shows, ideas, quotes, other links).

If the input is text, note that it may contain OCR-extracted text from screenshots (which shows YouTube search results containing video titles, channel names, view counts, and durations). Reconstruct these into YouTube video references.

Return a JSON object with this exact structure:
{
  "youtube_videos": [
    {
      "title": "best guess at video title",
      "channel": "channel name if mentioned",
      "direct_url": "full URL if explicitly given (or null)",
      "search_query": "optimized YT search query",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#tag1", "#tag2"]
    }
  ],
  "books": [
    {
      "title": "book title",
      "author": "author name if mentioned (or null)",
      "search_query": "title + author for search",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#tag1", "#tag2"]
    }
  ],
  "anime": [
    {
      "title": "anime title",
      "search_query": "anime title for search",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#anime"]
    }
  ],
  "manga": [
    {
      "title": "manga/manhwa title",
      "search_query": "manga title for search",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#manga"]
    }
  ],
  "movies_tv": [
    {
      "title": "movie or tv show title",
      "type": "MOVIE or TV_SHOW",
      "search_query": "title for search",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#movie"]
    }
  ],
  "ideas": [
    {
      "text": "the quote, thought, or idea",
      "author": "author or speaker if mentioned (or null)",
      "confidence": 0.0-1.0,
      "context": "sentence where this was mentioned",
      "tags": ["#quote"]
    }
  ],
  "other_links": [
    {
      "url": "full URL",
      "label": "what this link seems to be",
      "tags": ["#tag1", "#tag2"]
    }
  ],
  "summary": "one sentence describing this post"
}

Rules:
- If a video/book is mentioned without a URL, set direct_url/url to null and fill search_query.
- confidence 1.0 = exact URL given.
- confidence 0.8 = title clearly stated.
- confidence 0.5 = inferred from context.
- Categorize each item with 1-3 appropriate tags (e.g. #tech, #ai, #productivity, #finance, #books, #podcast, #design, #anime, #manga, #movies, #quotes).
- Return ONLY valid JSON. No markdown, no preamble.
"""

    # 1. Attempt Multimodal Vision first (if image_paths are provided)
    if image_paths:
        # A. Try Groq Multimodal Vision (Primary if <= 5 images)
        if GROQ_KEY and len(image_paths) <= 5:
            try:
                logger.info(f"Attempting multimodal Groq vision extraction on {len(image_paths)} images...")
                messages = [
                    {"role": "system", "content": SYSTEM_PROMPT}
                ]
                
                content_list = [
                    {"type": "text", "text": prompt_template + (f"\n\nTEXT:\n{raw_text}" if raw_text else "")}
                ]
                
                for path in image_paths:
                    base64_data = image_to_base64(path)
                    mime_type, _ = mimetypes.guess_type(path)
                    if not mime_type:
                        mime_type = "image/jpeg"
                    content_list.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_data}"
                        }
                    })
                
                messages.append({"role": "user", "content": content_list})
                
                response_text = await call_groq_with_quota(
                    messages,
                    model_name=GROQ_VISION_MODEL,
                    json_mode=True
                )
                
                cleaned_json = clean_json_text(response_text)
                parsed = json.loads(cleaned_json)
                cleaned_data = {
                    "youtube_videos": parsed.get("youtube_videos", []),
                    "books": parsed.get("books", []),
                    "other_links": parsed.get("other_links", []),
                    "anime": parsed.get("anime", []),
                    "manga": parsed.get("manga", []),
                    "movies_tv": parsed.get("movies_tv", []),
                    "ideas": parsed.get("ideas", []),
                    "summary": parsed.get("summary", "")
                }
                logger.info("Multimodal Groq extraction succeeded.")
                return GeminiExtractionResponse(**cleaned_data)
            except Exception as e:
                logger.warning(f"Multimodal Groq vision call failed: {e}. Trying Gemini fallback...")

        # B. Try Gemini Multimodal Vision (Secondary Fallback)
        if GEMINI_KEY:
            # Retry once for transient errors
            response_text = None
            for attempt in range(2):
                try:
                    pil_images = []
                    for path in image_paths:
                        try:
                            pil_images.append(Image.open(path))
                        except Exception as im_err:
                            logger.warning(f"Failed to open image {path} for Gemini: {im_err}")
                    
                    if pil_images:
                        prompt = prompt_template
                        if raw_text:
                            prompt += f"\n\nTEXT:\n{raw_text}"
                            
                        contents = pil_images + [prompt]
                        
                        response_text = await call_gemini_with_quota(
                            GEMINI_MODEL_NAME,
                            contents,
                            system_instruction=SYSTEM_PROMPT,
                            json_mode=True
                        )
                        
                        cleaned_json = clean_json_text(response_text)
                        parsed = json.loads(cleaned_json)
                        cleaned_data = {
                            "youtube_videos": parsed.get("youtube_videos", []),
                            "books": parsed.get("books", []),
                            "other_links": parsed.get("other_links", []),
                            "anime": parsed.get("anime", []),
                            "manga": parsed.get("manga", []),
                            "movies_tv": parsed.get("movies_tv", []),
                            "ideas": parsed.get("ideas", []),
                            "summary": parsed.get("summary", "")
                        }
                        logger.info("Multimodal Gemini extraction succeeded.")
                        return GeminiExtractionResponse(**cleaned_data)
                except Exception as e:
                    err_str = str(e)
                    if ("503" in err_str or "429" in err_str or "demand" in err_str.lower()) and attempt == 0:
                        logger.warning(f"Multimodal Gemini vision call failed with transient error: {e}. Retrying in 2.5s...")
                        await asyncio.sleep(2.5)
                    else:
                        logger.error(f"Multimodal Gemini vision call failed: {e}. Falling back to easyOCR...")
                        break
                        
        # C. If both vision models fail, run OCR on images to get raw text
        ocr_texts = []
        try:
            reader = get_ocr_reader()
            for path in image_paths:
                logger.info(f"Running easyOCR on image: {path}")
                ocr_result = reader.readtext(path, detail=0)
                if ocr_result:
                    ocr_texts.extend(ocr_result)
                else:
                    logger.warning(f"easyOCR returned empty for {path}")
        except Exception as ocr_err:
            logger.error(f"easyOCR process failed: {ocr_err}")
            
        raw_text = raw_text + "\n\n[OCR EXTRACTED TEXT]\n" + "\n".join(ocr_texts)

    # 2. Text-only extraction
    prompt = prompt_template
    if raw_text:
        prompt += f"\n\nTEXT:\n{raw_text}"

    # A. Try Groq text-only (Primary)
    if GROQ_KEY:
        try:
            logger.info("Running text-only Groq extraction...")
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            parsed = json.loads(cleaned_json)
            cleaned_data = {
                "youtube_videos": parsed.get("youtube_videos", []),
                "books": parsed.get("books", []),
                "other_links": parsed.get("other_links", []),
                "anime": parsed.get("anime", []),
                "manga": parsed.get("manga", []),
                "movies_tv": parsed.get("movies_tv", []),
                "ideas": parsed.get("ideas", []),
                "summary": parsed.get("summary", "")
            }
            logger.info("Text-only Groq extraction succeeded.")
            return GeminiExtractionResponse(**cleaned_data)
        except Exception as e:
            logger.warning(f"Text-only Groq extraction failed: {e}. Trying Gemini fallback...")

    # B. Try Gemini text-only (Secondary Fallback)
    if GEMINI_KEY:
        try:
            logger.info("Running text-only Gemini extraction...")
            response_text = await call_gemini_with_quota(
                GEMINI_MODEL_NAME,
                [prompt],
                system_instruction=SYSTEM_PROMPT,
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            parsed = json.loads(cleaned_json)
            cleaned_data = {
                "youtube_videos": parsed.get("youtube_videos", []),
                "books": parsed.get("books", []),
                "other_links": parsed.get("other_links", []),
                "anime": parsed.get("anime", []),
                "manga": parsed.get("manga", []),
                "movies_tv": parsed.get("movies_tv", []),
                "ideas": parsed.get("ideas", []),
                "summary": parsed.get("summary", "")
            }
            logger.info("Text-only Gemini extraction succeeded.")
            return GeminiExtractionResponse(**cleaned_data)
        except Exception as e:
            logger.error(f"First text-only Gemini extraction failed: {e}. Retrying with temp=0...")
            try:
                response_text = await call_gemini_with_quota(
                    GEMINI_MODEL_NAME,
                    [prompt + "\nIMPORTANT: respond in JSON only!"],
                    system_instruction=SYSTEM_PROMPT,
                    json_mode=True,
                    temperature=0.0
                )
                cleaned_json = clean_json_text(response_text)
                parsed = json.loads(cleaned_json)
                cleaned_data = {
                    "youtube_videos": parsed.get("youtube_videos", []),
                    "books": parsed.get("books", []),
                    "other_links": parsed.get("other_links", []),
                    "anime": parsed.get("anime", []),
                    "manga": parsed.get("manga", []),
                    "movies_tv": parsed.get("movies_tv", []),
                    "ideas": parsed.get("ideas", []),
                    "summary": parsed.get("summary", "")
                }
                logger.info("Text-only Gemini retry extraction succeeded.")
                return GeminiExtractionResponse(**cleaned_data)
            except Exception as retry_err:
                logger.error(f"Gemini extraction completely failed: {retry_err}. Falling back to Regex extraction.")

    # C. Fallback to Regex extraction if all else fails
    return fallback_extract(raw_text)

async def check_smart_dedup(new_title: str, existing_titles: List[str]) -> bool:
    """
    Asks AI if new_title matches any of the existing_titles semantically.
    Checks Groq first, falls back to Gemini.
    """
    if not existing_titles:
        return False
        
    prompt = f"""
You are a deduplication assistant. 
Compare the new content title: "{new_title}"
against the list of existing titles:
{json.dumps(existing_titles)}

Determine if the new title refers to the exact same video, book, or link as any item in the existing list (allowing for different casing, minor punctuation differences, added words like "Watch:", or subtitles).
CRITICAL: Do NOT hallucinate. Only flag as a duplicate if there is a CLEAR semantic match in the existing list.
If it is NOT a duplicate, you MUST set "is_duplicate" to false and "duplicate_title" to null.

Respond ONLY with a valid JSON object matching this structure:
{{
  "is_duplicate": true or false,
  "duplicate_title": "the matching title from the list, or null"
}}
"""

    # A. Try Gemini First for Dedup (More reliable logic/JSON adherence)
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(
                GEMINI_MODEL_NAME,
                [prompt],
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            data = json.loads(cleaned_json)
            is_dup = data.get("is_duplicate", False)
            if is_dup:
                logger.info(f"Smart Dedup (Gemini) matched: '{new_title}' semantically same as '{data.get('duplicate_title')}'")
            return is_dup
        except Exception as e:
            logger.warning(f"Gemini smart dedup failed: {e}. Trying Groq fallback...")

    # B. Try Groq Fallback
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            data = json.loads(cleaned_json)
            is_dup = data.get("is_duplicate", False)
            if is_dup:
                logger.info(f"Smart Dedup (Groq) matched: '{new_title}' semantically same as '{data.get('duplicate_title')}'")
            return is_dup
        except Exception as e:
            logger.error(f"Groq smart dedup failed: {e}")

    return False

async def check_smart_dedup_batch(new_titles: List[str], existing_titles: List[str]) -> Set[str]:
    """
    Asks AI to compare a list of new titles against existing titles in one call.
    Returns a set of new titles that are determined to be duplicates.
    Checks Groq first, falls back to Gemini.
    """
    if not new_titles or not existing_titles:
        return set()
        
    prompt = f"""
You are a deduplication assistant. 
We have a list of new content titles:
{json.dumps(new_titles)}

We want to check if any of these new titles refer to the exact same video, book, or link as any item in our list of existing titles:
{json.dumps(existing_titles)}

Determine which of the new titles are duplicates of an existing title (allowing for minor differences in casing, punctuation, spelling, added words like "Watch:", or subtitles).
CRITICAL: Do NOT hallucinate. Only flag as a duplicate if there is a CLEAR semantic match in the existing list.
If none of the new titles are duplicates, you MUST return an empty list: {{"duplicates": []}}.

Respond ONLY with a valid JSON object matching this structure:
{{
  "duplicates": [
    {{
      "new_title": "the new title that is a duplicate",
      "existing_title_match": "the matching title from the existing list"
    }}
  ]
}}
"""

    # A. Try Gemini First for Batch Dedup (More reliable logic/JSON adherence)
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(
                GEMINI_MODEL_NAME,
                [prompt],
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            data = json.loads(cleaned_json)
            
            duplicates = set()
            for item in data.get("duplicates", []):
                new_title = item.get("new_title")
                existing_match = item.get("existing_title_match")
                if new_title:
                    duplicates.add(new_title)
                    logger.debug(f"Smart Dedup Match: '{new_title}' matched with existing '{existing_match}'")
            logger.info(f"Batch Smart Dedup (Gemini) found {len(duplicates)} duplicates.")
            return duplicates
        except Exception as e:
            logger.warning(f"Gemini batch smart dedup failed: {e}. Trying Groq fallback...")

    # B. Try Groq Fallback
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=True
            )
            cleaned_json = clean_json_text(response_text)
            data = json.loads(cleaned_json)
            
            duplicates = set()
            for item in data.get("duplicates", []):
                new_title = item.get("new_title")
                existing_match = item.get("existing_title_match")
                if new_title:
                    duplicates.add(new_title)
                    logger.debug(f"Smart Dedup Match: '{new_title}' matched with existing '{existing_match}'")
            logger.info(f"Batch Smart Dedup (Groq) found {len(duplicates)} duplicates.")
            return duplicates
        except Exception as e:
            logger.error(f"Groq batch smart dedup failed: {e}")

    return set()

async def compose_reply_message(saved_items_summary: List[Dict[str, Any]], sheets_url: str) -> str:
    """
    Composes a natural, friendly Telegram message summarizing what was saved.
    Checks Groq first, falls back to Gemini.
    """
    prompt = f"""
Write a short, friendly Telegram message summarizing what was saved to the user's shelf.
Keep the response under 200 words.
Use emojis.
End the message with: "View your shelf: {sheets_url}"

Items saved:
{json.dumps(saved_items_summary)}
"""

    # A. Try Groq
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=False
            )
            return response_text.strip()
        except Exception as e:
            logger.warning(f"Groq reply composition failed: {e}. Trying Gemini...")

    # B. Try Gemini Fallback
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(GEMINI_MODEL_NAME, [prompt])
            return response_text.strip()
        except Exception as e:
            logger.error(f"Gemini reply composition failed: {e}")

    # Manual Fallback
    summary_lines = []
    for item in saved_items_summary:
        summary_lines.append(f"• {item.get('title')} ({item.get('type')})")
    items_str = "\n".join(summary_lines)
    return f"📥 Saved these items to your shelf:\n{items_str}\n\nView your shelf: {sheets_url}"

async def get_weekly_digest(items: List[Dict[str, Any]], sheets_url: str) -> str:
    """
    Generates a curated weekly digest from the list of saved shelf items.
    Checks Groq first, falls back to Gemini.
    """
    if not items:
        return "No items have been saved to your shelf in the last 7 days! Send me some Instagram links to build your shelf. 📚"
        
    prompt = f"""
Summarize these saved items into a weekly reading/watching list with recommendations on what to watch/read first.
Sort them nicely by content type.
Make it engaging, friendly, and structured.
End with a link to the Google Sheet: {sheets_url}

Items saved in the last 7 days:
{json.dumps(items)}
"""

    # A. Try Groq
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=False
            )
            return response_text.strip()
        except Exception as e:
            logger.warning(f"Groq weekly digest failed: {e}. Trying Gemini...")

    # B. Try Gemini Fallback
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(GEMINI_MODEL_NAME, [prompt])
            return response_text.strip()
        except Exception as e:
            logger.error(f"Gemini weekly digest failed: {e}")

    return "Failed to generate AI weekly digest. Try again later!"

async def search_shelf(query: str, items: List[Dict[str, Any]]) -> str:
    """
    Performs natural language search across items list using AI.
    Checks Groq first, falls back to Gemini.
    """
    if not items:
        return "Your shelf is currently empty."
        
    prompt = f"""
The user is searching for: "{query}"

From the list of shelf items below, find and list all items that are relevant to this query.
Explain your reasoning briefly for why each match was selected.
Be helpful and list the most relevant matches first with titles and URLs.
If no matches are found, say so politely.

Shelf items:
{json.dumps(items)}
"""

    # A. Try Groq
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=False
            )
            return response_text.strip()
        except Exception as e:
            logger.warning(f"Groq AI search failed: {e}. Trying Gemini...")

    # B. Try Gemini Fallback
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(GEMINI_MODEL_NAME, [prompt])
            return response_text.strip()
        except Exception as e:
            logger.error(f"Gemini AI search failed: {e}")

    return "Failed to complete AI search. Try again later!"

async def generate_notes_summary(video_title: str, notes_list: List[Dict[str, Any]]) -> str:
    """
    Generates a master summary from a list of user video notes.
    Uses Groq first, falls back to Gemini.
    """
    if not notes_list:
        return "No notes provided to generate a summary."
        
    prompt = f"""
You are an expert study assistant. The user has taken timestamped notes while watching the video: "{video_title}".
Here are their raw notes:
{json.dumps(notes_list)}

Your task is to take these raw notes and generate a highly descriptive, comprehensive, and engaging "Master Note".
REQUIREMENTS:
1. Organize the summary logically based on the progression of the video (using the timestamps as reference points).
2. Include fun doodles (using emojis or ascii art) to make it engaging.
3. Include structured graphs, flowcharts, or tables (using Markdown) to visualize the concepts discussed.
4. Expand on the user's brief notes with educational context if appropriate.
5. Format the entire response in beautiful Markdown.
"""

    # A. Try Groq
    if GROQ_KEY:
        try:
            messages = [
                {"role": "user", "content": prompt}
            ]
            response_text = await call_groq_with_quota(
                messages,
                model_name=GROQ_TEXT_MODEL,
                json_mode=False
            )
            return response_text.strip()
        except Exception as e:
            logger.warning(f"Groq generate notes summary failed: {e}. Trying Gemini...")

    # B. Try Gemini Fallback
    if GEMINI_KEY:
        try:
            response_text = await call_gemini_with_quota(GEMINI_MODEL_NAME, [prompt])
            return response_text.strip()
        except Exception as e:
            logger.error(f"Gemini generate notes summary failed: {e}")

    return "Failed to generate AI Master Note. Try again later!"
