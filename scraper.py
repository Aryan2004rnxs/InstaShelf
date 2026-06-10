import os
import re
import asyncio
import logging
import shutil
import glob
from typing import List, Tuple, Optional
from urllib.parse import urlparse
from apify_client import ApifyClientAsync
import httpx
import cv2

logger = logging.getLogger(__name__)

IG_REGEX = r'https?://(?:www\.)?instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)'

def extract_shortcode(url: str) -> Optional[Tuple[str, str]]:
    """
    Extracts the post type ('p', 'reel', 'tv') and shortcode from an Instagram URL.
    Returns (type, shortcode) or None if invalid.
    """
    try:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("p", "reel", "reels", "tv"):
            return "reel" if parts[0] == "reels" else parts[0], parts[1]
    except Exception as e:
        logger.error(f"Error parsing Instagram URL {url}: {e}")
    return None

def extract_video_keyframes(video_path: str, target_dir: str, interval_seconds: float = 2.0) -> List[str]:
    """
    Extracts keyframes from a video file using OpenCV.
    Saves them as JPG files in target_dir.
    """
    image_paths = []
    
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.warning(f"Could not open video file {video_path} for frame extraction.")
        return []
        
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0 # Default fallback
        
    frame_interval = int(fps * interval_seconds)
    frame_count = 0
    saved_count = 0
    max_frames = 15 # Safety limit to avoid sending too many images
    
    while cap.isOpened() and saved_count < max_frames:
        ret, frame = cap.read()
        if not ret:
            break
            
        if frame_count % frame_interval == 0:
            out_path = os.path.join(target_dir, f"frame_{saved_count:03d}.jpg")
            cv2.imwrite(out_path, frame)
            image_paths.append(out_path)
            saved_count += 1
            
        frame_count += 1
        
    cap.release()
    logger.info(f"Extracted {len(image_paths)} keyframes from video {video_path}")
    return image_paths

async def download_file(url: str, dest_path: str) -> bool:
    """Helper to download a file asynchronously"""
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            with open(dest_path, "wb") as f:
                f.write(response.content)
            return True
    except Exception as e:
        logger.error(f"Failed to download {url}: {e}")
        return False

async def scrape_instagram_content(url: str) -> Tuple[str, str, List[str], Optional[str]]:
    """
    Asynchronously extracts content from an Instagram URL using Apify.
    Returns: (source_type, caption, list_of_image_paths, temp_dir_to_clean)
    """
    apify_token = os.getenv("APIFY_API_TOKEN")
    if not apify_token or apify_token == "your_apify_api_token_here":
        raise RuntimeError("APIFY_API_TOKEN is not configured in the environment.")
        
    parsed = extract_shortcode(url)
    if not parsed:
        raise ValueError("Invalid Instagram URL format.")
        
    post_type, shortcode = parsed
    target_name = f"instashelf_{shortcode}"
    target_dir = os.path.abspath(target_name)
    
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    logger.info(f"Triggering Apify scraper for URL: {url}")
    
    client = ApifyClientAsync(apify_token)
    run_input = {
        "directUrls": [url],
        "resultsType": "details",
        "resultsLimit": 1,
        "addParentData": False,
        "searchType": "hashtag",
        "searchLimit": 1
    }
    
    try:
        # Run the apify/instagram-scraper actor
        run = await client.actor("apify/instagram-scraper").call(run_input=run_input)
        
        # Fetch the results from the dataset
        # Handle apify-client v2.5.1 returning a Pydantic model instead of dict
        dataset_id = getattr(run, "defaultDatasetId", getattr(run, "default_dataset_id", None))
        if not dataset_id:
            # Fallback for dicts just in case
            dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else None
        
        if not dataset_id:
            raise ValueError(f"Could not extract defaultDatasetId from run object: {run}")
            
        dataset_items = await client.dataset(dataset_id).list_items()
        items = dataset_items.items
        
        if not items:
            raise ValueError("Apify returned no data. The post might be private, deleted, or the URL is invalid.")
            
        post_data = items[0]
        
    except Exception as e:
        logger.error(f"Apify scraping failed: {e}")
        raise RuntimeError(f"Apify Instagram scraping failed: {str(e)}")

    caption = post_data.get("caption", "")
    image_paths = []
    
    # Apify schema can vary, so check multiple keys for video detection
    is_video = (
        post_data.get("isVideo", False) or 
        post_data.get("is_video", False) or 
        post_data.get("type") == "Video" or
        post_data.get("productType") == "clips" # sometimes reels are 'clips'
    )
    
    if is_video:
        video_url = post_data.get("videoUrl") or post_data.get("video_url")
        if video_url:
            video_path = os.path.join(target_dir, "video.mp4")
            success = await download_file(video_url, video_path)
            if success:
                # Extract keyframes using OpenCV
                image_paths = await asyncio.to_thread(extract_video_keyframes, video_path, target_dir, 2.0)
        else:
            logger.warning("Post was detected as a video, but no video URL was found in Apify data!")
        
        logger.info(f"Reel processed via Apify. Keyframes extracted: {len(image_paths)}, Caption length: {len(caption)}")
        return "REEL", caption, image_paths, target_dir
        
    else:
        # Image post or carousel
        media_urls = []
        if "images" in post_data and post_data["images"]:
            # Carousel
            media_urls = post_data["images"]
        elif "displayUrl" in post_data and post_data["displayUrl"]:
            # Single image
            media_urls = [post_data["displayUrl"]]
            
        for idx, m_url in enumerate(media_urls):
            img_path = os.path.join(target_dir, f"image_{idx:03d}.jpg")
            if await download_file(m_url, img_path):
                image_paths.append(img_path)
                
        logger.info(f"Post processed via Apify. Images downloaded: {len(image_paths)}, Caption length: {len(caption)}")
        return "POST", caption, image_paths, target_dir
