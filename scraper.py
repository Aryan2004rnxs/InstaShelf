import os
import re
import glob
import shutil
import asyncio
import logging
import instaloader
import urllib3
from typing import Optional, Tuple, List

# Suppress InsecureRequestWarning from urllib3 when verify=False is used
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

def get_proxies_list() -> List[str]:
    """
    Parse the SCRAPER_PROXY environment variable which could be a comma-separated list of proxies.
    Returns an empty list by default to disable free proxies per user request, testing direct connection.
    """
    return []

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

async def extract_reel_subtitles(url: str, shortcode: str, target_dir: Optional[str] = None) -> str:
    """
    Uses yt-dlp to extract auto-generated subtitles for the Reel without downloading the video.
    """
    import sys
    venv_ytdlp = os.path.join(sys.prefix, "bin", "yt-dlp")
    ytdlp_executable = venv_ytdlp if os.path.exists(venv_ytdlp) else "yt-dlp"

    dir_to_use = target_dir if target_dir else "/tmp"
    output_template = os.path.join(dir_to_use, f"subtitles_{shortcode}")
    # yt-dlp adds language and extension, e.g., /tmp/instashelf_{shortcode}.en.vtt
    expected_vtt_pattern = f"{output_template}.*.vtt"
    
    proxies_list = get_proxies_list()
    attempt_proxies = proxies_list + [None]
    
    for proxy in attempt_proxies:
        # Clean up any pre-existing files matching the pattern
        for path in glob.glob(expected_vtt_pattern):
            try:
                os.remove(path)
            except Exception:
                pass
                
        cmd = [
            ytdlp_executable,
            "--write-auto-sub",
            "--sub-lang", "en",
            "--skip-download",
            "--no-check-certificate",
            "--output", output_template
        ]
        
        if proxy:
            logger.info(f"Using scraper proxy for yt-dlp subtitle extraction: {proxy}")
            cmd.extend(["--proxy", proxy])
        else:
            logger.warning("Attempting yt-dlp subtitle extraction directly (no proxy).")
            
        cmd.append(url)
        
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
                logger.warning(f"yt-dlp subtitle extraction timed out for shortcode {shortcode} using proxy {proxy}")
                continue
                
            if process.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                logger.warning(f"yt-dlp failed or no auto-subs for shortcode {shortcode} using proxy {proxy}: {err_msg}")
                continue
                
            # Locate generated vtt files
            vtt_files = glob.glob(expected_vtt_pattern)
            if not vtt_files:
                logger.warning(f"No WebVTT subtitle files found for shortcode {shortcode} using proxy {proxy}")
                continue
                
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
            logger.error(f"Error running yt-dlp for shortcode {shortcode} with proxy {proxy}: {e}")
            continue
            
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
        post_metadata_txt_pattern="",  # Disable text file downloads
        max_connection_attempts=1,
        request_timeout=10
    )
    
    # Ignore Hugging Face container environment proxies/settings
    L.context._session.trust_env = False
    
    # Inject authenticated session cookies if provided (free bypass for rate blocks)
    session_id = os.getenv("INSTAGRAM_SESSION_ID")
    logger.info(f"Cookie exists: {bool(session_id)}")
    logger.info(f"Cookie length: {len(session_id) if session_id else 0}")
    if session_id:
        logger.info("Using Instagram authenticated session cookie for post scraping.")
        L.context._session.cookies.set("sessionid", session_id, domain=".instagram.com")
        dummy_csrf = "1234567890abcdef1234567890abcdef"
        L.context._session.cookies.set("csrftoken", dummy_csrf, domain=".instagram.com")
        L.context._session.headers.update({"X-CSRFToken": dummy_csrf})
        L.context.username = os.getenv("INSTAGRAM_USERNAME", "your_instagram_username")
    
    target_name = f"instashelf_{shortcode}"
    target_dir = os.path.abspath(target_name)
    
    proxies_list = get_proxies_list()
    attempt_proxies = proxies_list + [None]
    
    last_exception = None
    for proxy in attempt_proxies:
        if proxy:
            logger.info(f"Attempting post scraping using proxy: {proxy}")
            L.context._session.proxies = {
                "http": proxy,
                "https": proxy
            }
        else:
            logger.info("Attempting post scraping directly (no proxy)...")
            L.context._session.proxies = {
                "http": None,
                "https": None
            }
            
        try:
            # Clean up target directory if files were partially downloaded in a previous failed attempt
            if os.path.exists(target_dir):
                shutil.rmtree(target_dir)
            os.makedirs(target_dir, exist_ok=True)
            
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
        except ValueError as ve:
            raise ve
        except Exception as e:
            last_exception = e
            err_str = str(e)
            if "private" in err_str.lower() or "login" in err_str.lower():
                raise ValueError("This post is private or login is required. Only public posts work.")
            logger.warning(f"Post scraping failed with proxy {proxy}: {e}")
            
    # If we got here, all attempts failed
    logger.error(f"All post scraping attempts failed for shortcode {shortcode}. Last error: {last_exception}")
    raise RuntimeError(f"Instagram scraping failed: {last_exception}")

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
        compress_json=False,
        max_connection_attempts=1,
        request_timeout=10
    )
    
    # Ignore Hugging Face container environment proxies/settings
    L.context._session.trust_env = False
    
    # Inject authenticated session cookies if provided (free bypass for rate blocks)
    session_id = os.getenv("INSTAGRAM_SESSION_ID")
    logger.info(f"Cookie exists: {bool(session_id)}")
    logger.info(f"Cookie length: {len(session_id) if session_id else 0}")
    if session_id:
        logger.info("Using Instagram authenticated session cookie for reel scraping.")
        L.context._session.cookies.set("sessionid", session_id, domain=".instagram.com")
        dummy_csrf = "1234567890abcdef1234567890abcdef"
        L.context._session.cookies.set("csrftoken", dummy_csrf, domain=".instagram.com")
        L.context._session.headers.update({"X-CSRFToken": dummy_csrf})
        L.context.username = os.getenv("INSTAGRAM_USERNAME", "your_instagram_username")
    
    proxies_list = get_proxies_list()
    attempt_proxies = proxies_list + [None]
    
    last_exception = None
    for proxy in attempt_proxies:
        if proxy:
            logger.info(f"Attempting reel metadata scraping using proxy: {proxy}")
            L.context._session.proxies = {
                "http": proxy,
                "https": proxy
            }
        else:
            logger.info("Attempting reel metadata scraping directly (no proxy)...")
            L.context._session.proxies = {
                "http": None,
                "https": None
            }
            
        try:
            post = instaloader.Post.from_shortcode(L.context, shortcode)
            if post.owner_profile.is_private:
                raise ValueError("This post is private. Only public posts work.")
            return post.caption or ""
        except instaloader.exceptions.PrivateProfileNotFollowedException:
            raise ValueError("This post is private. Only public posts work.")
        except ValueError as ve:
            raise ve
        except Exception as e:
            last_exception = e
            err_str = str(e)
            if "private" in err_str.lower() or "login" in err_str.lower():
                raise ValueError("This post is private or login is required. Only public posts work.")
            logger.warning(f"Reel metadata scraping failed with proxy {proxy}: {e}")
            
    # If we got here, all attempts failed
    logger.error(f"All reel metadata scraping attempts failed for shortcode {shortcode}. Last error: {last_exception}")
    raise RuntimeError(f"Instagram reel scraping failed: {last_exception}")

async def download_reel_video(url: str, shortcode: str, target_dir: str) -> Optional[str]:
    """
    Downloads the Reel video using yt-dlp.
    Returns the path to the downloaded MP4 file, or None if download fails.
    """
    import sys
    venv_ytdlp = os.path.join(sys.prefix, "bin", "yt-dlp")
    ytdlp_executable = venv_ytdlp if os.path.exists(venv_ytdlp) else "yt-dlp"
    
    video_output = os.path.join(target_dir, "video.mp4")
    
    proxies_list = get_proxies_list()
    attempt_proxies = proxies_list + [None]
    
    for proxy in attempt_proxies:
        # Clean up any pre-existing video files
        for path in glob.glob(os.path.join(target_dir, "video.*")):
            try:
                os.remove(path)
            except Exception:
                pass
                
        cmd = [
            ytdlp_executable,
            "--format", "mp4/best",
            "--no-check-certificate",
            "--output", video_output
        ]
        
        if proxy:
            logger.info(f"Using scraper proxy for yt-dlp video download: {proxy}")
            cmd.extend(["--proxy", proxy])
        else:
            logger.warning("Attempting yt-dlp video download directly (no proxy).")
            
        cmd.append(url)
        
        logger.info(f"Downloading Reel video using yt-dlp for shortcode: {shortcode}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except Exception:
                    pass
                await process.communicate()
                logger.warning(f"yt-dlp video download timed out for shortcode {shortcode} using proxy {proxy}")
                continue
                
            if process.returncode != 0:
                err_msg = stderr.decode(errors="replace").strip()
                logger.warning(f"yt-dlp video download failed for shortcode {shortcode} using proxy {proxy}: {err_msg}")
                continue
                
            if os.path.exists(video_output):
                return video_output
                
            # Check if it was downloaded with a slightly different extension/format name
            files = glob.glob(os.path.join(target_dir, "video.*"))
            if files:
                return files[0]
                
        except Exception as e:
            logger.error(f"Error running yt-dlp video download for shortcode {shortcode} with proxy {proxy}: {e}")
            continue
            
    return None

def extract_video_keyframes(video_path: str, target_dir: str, interval_seconds: float = 2.0) -> List[str]:
    """
    Extracts keyframes from a video file using OpenCV.
    Saves them as JPG files in target_dir.
    """
    import cv2
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

async def scrape_instagram_content(url: str) -> Tuple[str, str, List[str], Optional[str]]:
    """
    Asynchronously extracts content from an Instagram URL.
    Returns: (source_type, caption, list_of_image_paths, temp_dir_to_clean)
    
    If source_type is REEL:
      Downloads subtitles using yt-dlp, appends to caption.
      Also downloads video and extracts keyframes for multimodal vision processing.
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
        
        # Set up a target directory to store downloaded video and frames
        target_name = f"instashelf_{shortcode}"
        target_dir = os.path.abspath(target_name)
        if os.path.exists(target_dir):
            shutil.rmtree(target_dir)
        os.makedirs(target_dir, exist_ok=True)
        
        # Try to download video and extract keyframes
        video_path = await download_reel_video(url, shortcode, target_dir)
        image_paths = []
        if video_path:
            # Extract 1 frame every 2.0 seconds
            image_paths = await asyncio.to_thread(
                extract_video_keyframes, video_path, target_dir, 2.0
            )
            
        # Fetch subtitles asynchronously (fallback/complementary text)
        subtitles = await extract_reel_subtitles(url, shortcode, target_dir)
        
        raw_text = caption
        if subtitles:
            raw_text += "\n\n[SUBTITLES]\n" + subtitles
            
        logger.info(f"Reel processed. Keyframes extracted: {len(image_paths)}, Raw text length: {len(raw_text)}")
        return "REEL", raw_text, image_paths, target_dir
    else:
        # For image posts (/p/)
        caption, image_paths, temp_dir = await asyncio.to_thread(_scrape_post_images_sync, shortcode)
        logger.info(f"Post processed. Carousel images: {len(image_paths)}, Caption length: {len(caption)}")
        return "POST", caption, image_paths, temp_dir
