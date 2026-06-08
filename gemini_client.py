import os
import json
import logging
import asyncio
from typing import List, Optional, Dict, Any
from PIL import Image
import google.generativeai as genai
from models import GeminiExtractionResponse
from utils import increment_gemini_usage, get_gemini_usage
from fallback_extractor import fallback_extract

logger = logging.getLogger("InstaShelf.gemini")

# Initialize Gemini
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_KEY:
    genai.configure(api_key=GEMINI_KEY)
else:
    logger.warning("GEMINI_API_KEY is not set in environment variables.")

# Model configuration
MODEL_NAME = "gemini-2.5-flash"
SYSTEM_PROMPT = (
    "You are a content extraction assistant. Your job is to analyze text or images from "
    "Instagram posts and identify all references to YouTube videos, books, podcasts, courses, "
    "and useful links."
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

async def call_gemini_with_quota(model, contents: List[Any], generation_config: Dict[str, Any] = None) -> str:
    """
    Wrapper for model.generate_content that checks and tracks the 1500 daily quota.
    Raises RuntimeError if quota is exceeded.
    """
    usage = get_gemini_usage()
    if usage >= 1500:
        logger.warning(f"Gemini API quota exceeded for today ({usage}/1500).")
        raise RuntimeError("Quota Limit Exceeded")
        
    increment_gemini_usage()
    
    # Run the Gemini API call in a thread pool since it's blocking
    loop = asyncio.get_running_loop()
    if generation_config:
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                contents,
                generation_config=generation_config,
                request_options=genai.types.RequestOptions(timeout=15.0)
            )
        )
    else:
        response = await loop.run_in_executor(
            None,
            lambda: model.generate_content(
                contents,
                request_options=genai.types.RequestOptions(timeout=15.0)
            )
        )
        
    return response.text

async def extract_content_with_gemini(raw_text: str, image_paths: List[str] = None) -> GeminiExtractionResponse:
    """
    Main entry point for AI extraction.
    For POST:
      Attempts multimodal vision extraction first.
      If it fails, runs easyOCR on images and runs text-only extraction.
    For REEL:
      Runs text-only extraction on caption + subtitles.
      
    Falls back to regex extraction (fallback_extractor.py) if Gemini fails or quota is hit.
    """
    prompt_template = """Analyze this text or images from an Instagram post and extract ALL references to external content (YouTube videos, books, courses, other links).

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
- Categorize each item with 1-3 appropriate tags (e.g. #tech, #ai, #productivity, #finance, #books, #podcast, #design).
- Return ONLY valid JSON. No markdown, no preamble.
"""

    # Attempt Multimodal if images are present (Vision is PRIMARY)
    if image_paths:
        logger.info(f"Attempting multimodal Gemini vision extraction on {len(image_paths)} images...")
        
        # Retry once for transient 503/429 errors
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
                    model = genai.GenerativeModel(
                        model_name=MODEL_NAME,
                        system_instruction=SYSTEM_PROMPT
                    )
                    
                    # Construct prompt for multimodal
                    prompt = prompt_template
                    if raw_text:
                        prompt += f"\n\nTEXT:\n{raw_text}"
                        
                    contents = pil_images + [prompt]
                    
                    response_text = await call_gemini_with_quota(
                        model,
                        contents,
                        generation_config={"response_mime_type": "application/json"}
                    )
                    
                    cleaned_json = clean_json_text(response_text)
                    parsed = json.loads(cleaned_json)
                    logger.info("Multimodal extraction succeeded.")
                    return GeminiExtractionResponse(**parsed)
            except Exception as e:
                err_str = str(e)
                if ("503" in err_str or "429" in err_str or "demand" in err_str.lower()) and attempt == 0:
                    logger.warning(f"Multimodal vision call failed with transient error: {e}. Retrying in 2.5 seconds...")
                    await asyncio.sleep(2.5)
                else:
                    logger.error(f"Multimodal vision call failed: {e}. Falling back to easyOCR + text Gemini...")
                    break
            
        # If multimodal fails, fall back to OCR + text extraction
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
        
    # Text-only Gemini extraction
    logger.info("Running text-only Gemini extraction...")
    
    # Construct final prompt with raw_text (which includes OCR if fallback happened)
    prompt = prompt_template
    if raw_text:
        prompt += f"\n\nTEXT:\n{raw_text}"
        
    try:
        model = genai.GenerativeModel(
            model_name=MODEL_NAME,
            system_instruction=SYSTEM_PROMPT
        )
        
        # Try once with system prompt & json instruction
        response_text = await call_gemini_with_quota(
            model,
            [prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        cleaned_json = clean_json_text(response_text)
        parsed = json.loads(cleaned_json)
        return GeminiExtractionResponse(**parsed)
    except Exception as e:
        logger.error(f"First text-only Gemini extraction failed: {e}. Retrying with temp=0...")
        # Retry once with temperature=0
        try:
            model = genai.GenerativeModel(
                model_name=MODEL_NAME,
                system_instruction=SYSTEM_PROMPT
            )
            response_text = await call_gemini_with_quota(
                model,
                [prompt + "\nIMPORTANT: respond in JSON only!"],
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.0
                }
            )
            cleaned_json = clean_json_text(response_text)
            parsed = json.loads(cleaned_json)
            return GeminiExtractionResponse(**parsed)
        except Exception as retry_err:
            logger.error(f"Gemini extraction completely failed: {retry_err}. Falling back to Regex extraction.")
            # Final fallback to regex extraction
            return fallback_extract(raw_text)

async def check_smart_dedup(new_title: str, existing_titles: List[str]) -> bool:
    """
    Asks Gemini if new_title matches any of the existing_titles semantically.
    """
    if not existing_titles:
        return False
        
    # Rate limit safety delay for Gemini Free Tier (15 Requests Per Minute limit)
    await asyncio.sleep(2.0)
    
    prompt = f"""
You are a deduplication assistant. 
Compare the new content title: "{new_title}"
against the list of existing titles:
{json.dumps(existing_titles)}

Determine if the new title refers to the exact same video, book, or link as any item in the existing list (allowing for different casing, minor punctuation differences, added words like "Watch:", or subtitles).

Respond ONLY with a valid JSON object matching this structure:
{{
  "is_duplicate": true or false,
  "duplicate_title": "the matching title from the list, or null"
}}
"""
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        response_text = await call_gemini_with_quota(
            model,
            [prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        cleaned_json = clean_json_text(response_text)
        data = json.loads(cleaned_json)
        is_dup = data.get("is_duplicate", False)
        if is_dup:
            logger.info(f"Smart Dedup matched: '{new_title}' is semantically same as '{data.get('duplicate_title')}'")
        return is_dup
    except Exception as e:
        logger.error(f"Smart Dedup check failed: {e}")
        return False

async def compose_reply_message(saved_items_summary: List[Dict[str, Any]], sheets_url: str) -> str:
    """
    Composes a natural, friendly Telegram message summarizing what was saved.
    """
    prompt = f"""
Write a short, friendly Telegram message summarizing what was saved to the user's shelf.
Keep the response under 200 words.
Use emojis.
End the message with: "View your shelf: {sheets_url}"

Items saved:
{json.dumps(saved_items_summary)}
"""
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        # Call Gemini (does not need JSON constraints)
        response_text = await call_gemini_with_quota(model, [prompt])
        return response_text.strip()
    except Exception as e:
        logger.error(f"Reply composition failed: {e}")
        # Manual fallback
        summary_lines = []
        for item in saved_items_summary:
            summary_lines.append(f"• {item.get('title')} ({item.get('type')})")
        items_str = "\n".join(summary_lines)
        return f"📥 Saved these items to your shelf:\n{items_str}\n\nView your shelf: {sheets_url}"

async def get_weekly_digest(items: List[Dict[str, Any]], sheets_url: str) -> str:
    """
    Generates a curated weekly digest from the list of saved shelf items.
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
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        response_text = await call_gemini_with_quota(model, [prompt])
        return response_text.strip()
    except Exception as e:
        logger.error(f"Digest generation failed: {e}")
        return "Failed to generate AI weekly digest. Try again later!"

async def search_shelf(query: str, items: List[Dict[str, Any]]) -> str:
    """
    Performs natural language search across items list using Gemini.
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
    try:
        model = genai.GenerativeModel(model_name=MODEL_NAME)
        response_text = await call_gemini_with_quota(model, [prompt])
        return response_text.strip()
    except Exception as e:
        logger.error(f"AI Search failed: {e}")
        return "Failed to complete AI search. Try again later!"
