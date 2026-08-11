"""
fb_downloader.py — Facebook Video Downloader Module for SML-Mirror Bot

Provides a clean, swappable Facebook video extraction API.
Uses fbdown.blog backend by default. To change the API,
modify FB_API_URL, FB_API_METHOD, and _parse_response() below.

Author: CodeNinjaXd | Channel: @NullError_XD
"""

from json import loads as json_loads
from typing import Dict

from requests import post as http_post
from requests.exceptions import RequestException

from ...ext_utils.exceptions import DirectDownloadLinkException


# ── FB API Configuration (swappable) ───────────────────────────────────────
#   Override these via Config or change the values here.
#   The module is self-contained; no other file needs changes to swap APIs.
# ────────────────────────────────────────────────────────────────────────────

FB_API_URL = "https://fbdown.blog/get.php"
FB_API_METHOD = "POST"  # POST or GET
FB_REQUEST_TIMEOUT = 30  # seconds

# User-Agent used for API requests
FB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _parse_response(response_text: str, original_url: str) -> Dict:
    """
    Parse the fbdown.blog API response.

    Override THIS function when swapping to a different FB API.
    It must return a dict with at least: {"hd": str, "sd": str, "title": str}
    """
    try:
        data = json_loads(response_text)
    except ValueError:
        raise DirectDownloadLinkException(
            "ERROR: Facebook API returned invalid JSON. The service may be down."
        )

    if not isinstance(data, dict):
        raise DirectDownloadLinkException(
            "ERROR: Facebook API returned unexpected data format."
        )

    # Check for API-level errors (fbdown.blog uses "error" key, others may use "status")
    api_error = data.get("error") or data.get("message")
    if api_error and isinstance(api_error, str) and api_error.strip():
        raise DirectDownloadLinkException(f"ERROR: Facebook API — {api_error}")
    if data.get("status") == "error":
        msg = data.get("message", "Unknown API error")
        raise DirectDownloadLinkException(f"ERROR: Facebook API — {msg}")

    result_data = data.get("data")
    if not result_data or not isinstance(result_data, dict):
        raise DirectDownloadLinkException(
            "ERROR: No video data found. The Facebook link may be private, "
            "deleted, or invalid."
        )

    # Extract video qualities — only accept recognized quality labels
    hd_url = ""
    sd_url = ""
    VALID_QUALITIES = {"HD", "SD"}

    medias = result_data.get("medias", [])
    if isinstance(medias, list):
        for media in medias:
            if not isinstance(media, dict):
                continue
            quality = media.get("quality", "").upper().strip()
            url = media.get("url", "")
            if quality not in VALID_QUALITIES or not url:
                continue
            if quality == "HD" and not hd_url:
                hd_url = url
            elif quality == "SD" and not sd_url:
                sd_url = url

    # Fallback: if no medias array, check for direct url fields
    if not hd_url and not sd_url:
        hd_url = result_data.get("url") or result_data.get("videoUrl") or ""
        sd_url = hd_url  # same as HD if only one quality

    if not hd_url:
        raise DirectDownloadLinkException(
            "ERROR: Could not extract any video URL from Facebook API response."
        )

    # Duration formatting
    duration_sec = result_data.get("duration", 0)
    if isinstance(duration_sec, (int, float)) and duration_sec > 0:
        mins = int(duration_sec) // 60
        secs = int(duration_sec) % 60
        duration_str = f"{mins:02d}:{secs:02d}"
    else:
        duration_str = "Unknown"

    return {
        "hd": hd_url,
        "sd": sd_url or hd_url,
        "title": result_data.get("title", "Facebook Video"),
        "author": result_data.get("author", "Unknown"),
        "duration": duration_str,
        "thumbnail": result_data.get("thumbnail", ""),
    }


def extract_fb_video(url: str) -> Dict[str, str]:
    """
    Extract Facebook video download links.

    Args:
        url: Full Facebook video/post URL.

    Returns:
        Dict with keys: hd, sd, title, author, duration, thumbnail

    Raises:
        DirectDownloadLinkException on any failure.
    """
    if not url:
        raise DirectDownloadLinkException("ERROR: No Facebook URL provided.")

    headers = {
        "User-Agent": FB_USER_AGENT,
        "Origin": "https://fbdown.blog",
        "Referer": "https://fbdown.blog/",
        "Accept": "application/json, text/plain, */*",
    }

    try:
        if FB_API_METHOD.upper() == "POST":
            resp = http_post(
                FB_API_URL,
                data={"url": url},
                headers=headers,
                timeout=FB_REQUEST_TIMEOUT,
            )
        else:
            from requests import get as http_get
            resp = http_get(
                FB_API_URL,
                params={"url": url},
                headers=headers,
                timeout=FB_REQUEST_TIMEOUT,
            )

        resp.raise_for_status()
        return _parse_response(resp.text, url)

    except RequestException as e:
        raise DirectDownloadLinkException(
            f"ERROR: Facebook API request failed — {e}"
        )
    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(
            f"ERROR: Unexpected error while fetching Facebook video — {e}"
        )


def get_fb_direct_link(url: str):
    """
    Extract Facebook video and return a multi-file dict for direct download.

    Returns both HD and SD qualities when available, so the bot downloads
    and mirrors/leeches both formats in a single task.

    Returns:
        Dict with keys: contents, title, total_size, header
        - contents: list of {path, filename, url} dicts
        - title: folder name for the download
        - header: headers string for Facebook CDN access

        Compatible with add_direct_download() in direct_downloader.py

    Raises:
        DirectDownloadLinkException on failure.
    """
    import re as _re

    info = extract_fb_video(url)

    # Sanitize title for filename use
    raw_title = info["title"].strip() or "Facebook_Video"
    safe_title = _re.sub(r"[^\w.\- ]", "", raw_title).strip()[:80]

    author = info["author"].strip() or "Unknown"
    safe_author = _re.sub(r"[^\w.\- ]", "", author).strip()[:40]

    folder_name = f"Facebook - {safe_author} - {safe_title[:60]}"

    fb_headers = (
        f"User-Agent: {FB_USER_AGENT}\n"
        "Referer: https://www.facebook.com/\n"
        "Accept: */*"
    )

    contents = []

    # HD quality
    if info["hd"]:
        contents.append({
            "path": folder_name,
            "filename": f"{safe_title[:60]}_HD.mp4",
            "url": info["hd"],
        })

    # SD quality (only if different from HD URL)
    if info["sd"] and info["sd"] != info.get("hd", ""):
        contents.append({
            "path": folder_name,
            "filename": f"{safe_title[:60]}_SD.mp4",
            "url": info["sd"],
        })

    if not contents:
        raise DirectDownloadLinkException(
            "ERROR: No downloadable video URLs found for this Facebook link."
        )

    return {
        "contents": contents,
        "title": folder_name,
        "total_size": 0,
        "header": fb_headers,
    }


def is_facebook_url(url: str) -> bool:
    """Check if a URL is a Facebook video link."""
    if not url:
        return False
    from urllib.parse import urlparse
    try:
        domain = urlparse(url).hostname or ""
        return any(
            fb_domain in domain
            for fb_domain in ("facebook.com", "fb.com", "fb.watch", "fb.me")
        )
    except Exception:
        return False
