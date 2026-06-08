import os
import re
import glob
import shutil
import asyncio
import logging
import instaloader
from typing import Optional, Tuple, List

logger = logging.getLogger("InstaShelf.scraper")

# Instagram URL parsing regex
IG_REGEX = r'https?://(?:www\.)?instagram\.com/(p|reel|tv)/([A-Za-z0-9_-]+)'

def extract_shortcode(url: str) -> Optional[Tuple[str, str]]:
    """
    Extracts the post type ('p', 'reel', 'tv') and shortcode from an Instagram URL.
    Returns (post_type, shortcode) or None if URL is not a match.
    """
    match = re.search(IG_REGEX, url)
    if match:
        return match.group(1), match.group(2)
    return None

def parse_vtt_to_text(vtt_content: str) -> str:
    """
    Parses a WebVTT file content to clean plain text.
    Strips headers, timestamps, HTML/XML tags, and removes rolling duplicate lines.
    """
    lines = vtt_content.splitlines()
    cleaned_lines = []
    
    for line in lines:
        line = line.strip()
        # Ignore empty lines, VTT headers, and timestamp lines
        if not line or line.startswith("WEBVTT") or "-->" in line or line.startswith("NOTE"):
            continue
        # Strip XML-like style tags, e.g. <c>text</c>
        line = re.sub(r"<[^>]+>", "", line)
        if line:
            cleaned_lines.append(line)
            
    # Deduplicate repeating consecutive lines (common in rolling auto-generated subtitles)
    deduped_lines = []
    for line in cleaned_lines:
        if not deduped_lines or deduped_lines[-1] != line:
            deduped_lines.append(line)
            
    return " ".join(deduped_lines)

async def extract_reel_subtitles(url: str, shortcode: str) -> str:
    """
    Uses yt-dlp to extract auto-generated subtitles for the Reel without downloading the video.
    """
    output_template = f"/tmp/instashelf_{shortcode}"
    # yt-dlp adds language and extension, e.g., /tmp/instashelf_{shortcode}.en.vtt
    expected_vtt_pattern = f"{output_template}.*.vtt"
    
    # Clean up any pre-existing files matching the pattern
    for path in glob.glob(expected_vtt_pattern):
        try:
            os.remove(path)
        except Exception:
            pass
            
    cmd = [
        "yt-dlp",
        "--no-interactive",
        "--write-auto-sub",
        "--sub-lang", "en",
        "--skip-download",
        "--output", output_template,
        url
    ]
    
    logger.info(f"Running yt-dlp subtitle extraction for shortcode: {shortcode}")
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45.0)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except Exception:
                pass
            stdout, stderr = await process.communicate()
            logger.warning(f"yt-dlp subtitle extraction timed out for shortcode {shortcode}")
            return ""
            
        if process.returncode != 0:
            err_msg = stderr.decode(errors="replace").strip()
            logger.warning(f"yt-dlp failed or no auto-subs for shortcode {shortcode}: {err_msg}")
            return ""
            
        # Locate generated vtt files
        vtt_files = glob.glob(expected_vtt_pattern)
        if not vtt_files:
            logger.warning(f"No WebVTT subtitle files found for shortcode {shortcode}")
            return ""
            
        vtt_file_path = vtt_files[0]
        logger.info(f"Found WebVTT file: {vtt_file_path}")
        
        with open(vtt_file_path, "r", encoding="utf-8") as f:
            vtt_content = f.read()
            
        # Clean up the file
        try:
            os.remove(vtt_file_path)
        except Exception as ce:
            logger.warning(f"Failed to remove subtitle file {vtt_file_path}: {ce}")
            
        return parse_vtt_to_text(vtt_content)
    except Exception as e:
        logger.error(f"Error running yt-dlp for shortcode {shortcode}: {e}")
        return ""

def _scrape_post_images_sync(shortcode: str) -> Tuple[str, List[str], str]:
    """
    Synchronous helper to fetch post metadata and download all carousel images.
    """
    # Configure instaloader
    L = instaloader.Instaloader(
        download_pictures=True,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        post_metadata_txt_pattern=""  # Disable text file downloads
    )
    
    target_name = f"instashelf_{shortcode}"
    target_dir = os.path.abspath(target_name)
    if os.path.exists(target_dir):
        shutil.rmtree(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        # Check if post is private
        if post.owner_profile.is_private:
            raise ValueError("This post is private. Only public posts work.")
            
        caption = post.caption or ""
        
        # Download images
        L.download_post(post, target=target_name)
        
        # Gather all downloaded image file paths
        image_paths = []
        for ext in ("*.jpg", "*.jpeg", "*.png"):
            image_paths.extend(glob.glob(os.path.join(target_dir, ext)))
            
        image_paths.sort()
        logger.info(f"Successfully downloaded {len(image_paths)} images for shortcode {shortcode}")
        return caption, image_paths, target_dir
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise ValueError("This post is private. Only public posts work.")
    except Exception as e:
        # Check if error indicates private post
        err_str = str(e)
        if "private" in err_str.lower() or "login" in err_str.lower():
            raise ValueError("This post is private or login is required. Only public posts work.")
        logger.error(f"Instaloader failed for shortcode {shortcode}: {e}")
        raise RuntimeError(f"Instagram scraping failed: {err_str}")

def _scrape_reel_metadata_sync(shortcode: str) -> str:
    """
    Synchronous helper to fetch reel metadata (caption only).
    """
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False
    )
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        if post.owner_profile.is_private:
            raise ValueError("This post is private. Only public posts work.")
        return post.caption or ""
    except instaloader.exceptions.PrivateProfileNotFollowedException:
        raise ValueError("This post is private. Only public posts work.")
    except Exception as e:
        err_str = str(e)
        if "private" in err_str.lower() or "login" in err_str.lower():
            raise ValueError("This post is private or login is required. Only public posts work.")
        logger.error(f"Instaloader failed for reel shortcode {shortcode}: {e}")
        raise RuntimeError(f"Instagram reel scraping failed: {err_str}")

async def scrape_instagram_content(url: str) -> Tuple[str, str, List[str], Optional[str]]:
    """
    Asynchronously extracts content from an Instagram URL.
    Returns: (source_type, caption, list_of_image_paths, temp_dir_to_clean)
    
    If source_type is REEL:
      Downloads subtitles using yt-dlp, appends to caption, returns it.
    If source_type is POST:
      Downloads all images, returns list of paths and temp_dir for later cleanup.
    """
    parsed = extract_shortcode(url)
    if not parsed:
        raise ValueError("Invalid Instagram URL format.")
        
    post_type, shortcode = parsed
    
    if post_type in ("reel", "tv"):
        # Fetch caption asynchronously
        caption = await asyncio.to_thread(_scrape_reel_metadata_sync, shortcode)
        # Fetch subtitles asynchronously
        subtitles = await extract_reel_subtitles(url, shortcode)
        
        raw_text = caption
        if subtitles:
            raw_text += "\n\n[SUBTITLES]\n" + subtitles
            
        logger.info(f"Reel processed. Raw text length: {len(raw_text)}")
        return "REEL", raw_text, [], None
    else:
        # For image posts (/p/)
        caption, image_paths, temp_dir = await asyncio.to_thread(_scrape_post_images_sync, shortcode)
        logger.info(f"Post processed. Carousel images: {len(image_paths)}, Caption length: {len(caption)}")
        return "POST", caption, image_paths, temp_dir
