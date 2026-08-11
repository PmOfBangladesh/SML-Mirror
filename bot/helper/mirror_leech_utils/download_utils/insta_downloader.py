"""
insta_downloader.py — Instagram Downloader Module for SML-Mirror Bot

Uses FastDL.app API to extract Instagram media (videos, reels, photos,
carousels, stories, IGTV). Requires subprocess+curl due to JA3 TLS
fingerprint protection on the FastDL API.

To swap the backend, modify FASTDL_API_URL, HMAC_KEY, and _parse_fastdl_response().

Author: CodeNinjaXd | Channel: @NullError_XD
"""

import hashlib
import hmac
import json as json_lib
import re as _re
import subprocess
import time
import urllib.parse
from typing import Dict, List

from ...ext_utils.exceptions import DirectDownloadLinkException


# ── FastDL API Configuration (swappable) ────────────────────────────────────

FASTDL_API_URL = "https://api-wh.fastdl.app/api/convert"
HMAC_KEY_HEX = "28e8ce51a2d0481641dafda7028af488ec14e90ff4aa06823f53b2037d5299a9"
HMAC_KEY_BYTES = bytes.fromhex(HMAC_KEY_HEX)
CURL_TIMEOUT = 20  # seconds

CURL_CMD_TEMPLATE = [
    "curl", "-sL", FASTDL_API_URL,
    "-H", "accept: application/json, text/plain, */*",
    "-H", "accept-language: en-US",
    "-H", "content-type: application/x-www-form-urlencoded;charset=UTF-8",
    "-H", "origin: https://fastdl.app",
    "-H", "priority: u=1, i",
    "-H", "referer: https://fastdl.app/",
    "-H", "sec-ch-ua: \"Chromium\";v=\"127\", \"Not)A;Brand\";v=\"99\"",
    "-H", "sec-ch-ua-mobile: ?1",
    "-H", "sec-ch-ua-platform: \"Android\"",
    "-H", "sec-fetch-dest: empty",
    "-H", "sec-fetch-mode: cors",
    "-H", "sec-fetch-site: same-site",
    "-H", (
        "user-agent: Mozilla/5.0 (Linux; Android 10; K) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Mobile Safari/537.36"
    ),
]


def _generate_fastdl_payload(url: str) -> str:
    """Generate HMAC-signed payload for FastDL API."""
    ts = str(int(time.time() * 1000))
    message = url + ts
    sig = hmac.new(HMAC_KEY_BYTES, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"sf_url={urllib.parse.quote_plus(url)}"
        f"&ts={ts}"
        f"&_ts=1783424223289"
        f"&_tsc=0"
        f"&_sv=2"
        f"&_s={sig}"
    )


def _parse_fastdl_response(raw_json: str, original_url: str) -> Dict:
    """
    Parse FastDL API JSON response into standardized format.

    Override this function when swapping to a different IG API.
    Must return: {"links": [str], "thumbnails": [str], "title": str,
                   "items_count": int, "media_type": str}
    """
    try:
        data = json_lib.loads(raw_json)
    except (json_lib.JSONDecodeError, ValueError):
        raise DirectDownloadLinkException(
            "ERROR: Instagram API returned invalid JSON."
        )

    items = data if isinstance(data, list) else [data]
    if not items:
        raise DirectDownloadLinkException(
            "ERROR: No media found for this Instagram link."
        )

    all_links: List[str] = []
    all_thumbs: List[str] = []
    titles: List[str] = []

    for item in items:
        # Thumbnails
        thumb = item.get("thumb")
        if thumb:
            all_thumbs.append(thumb)

        # Title from meta
        meta = item.get("meta", {})
        title = meta.get("title", "")
        if title:
            titles.append(title)

        # Extract URLs
        url_list = item.get("url", [])
        for entry in url_list:
            link = entry.get("url", "") if isinstance(entry, dict) else entry
            if link:
                all_links.append(link)

    if not all_links:
        raise DirectDownloadLinkException(
            "ERROR: Could not extract any download URLs from Instagram API."
        )

    # Deduplicate
    unique_links: Dict[str, str] = {}
    for u in all_links:
        # Try to extract a unique file identifier
        m = _re.search(r"filename=([a-zA-Z0-9_-]+\.[a-zA-Z0-9]+)", u)
        if m:
            unique_links[m.group(1)] = u
        else:
            m = _re.search(r"([0-9]{8,15}_[0-9]{12,20}_[0-9]{12,20})", u)
            if m:
                unique_links[m.group(1)] = u
            else:
                unique_links[u] = u

    # Determine media type
    media_type = "carousel" if len(items) > 1 else "media"

    return {
        "links": list(unique_links.values()),
        "thumbnails": list(dict.fromkeys(all_thumbs)),
        "title": titles[0] if titles else "Instagram Media",
        "items_count": len(items),
        "media_type": media_type,
    }


def extract_instagram_media(url: str) -> Dict:
    """
    Extract Instagram media download links via FastDL API.

    Args:
        url: Full Instagram post/reel/story URL.

    Returns:
        Dict with keys: links, thumbnails, title, items_count, media_type

    Raises:
        DirectDownloadLinkException on failure.
    """
    if not url:
        raise DirectDownloadLinkException("ERROR: No Instagram URL provided.")

    payload = _generate_fastdl_payload(url)

    # Build curl command — append --data-raw at the end
    cmd = list(CURL_CMD_TEMPLATE) + ["--data-raw", payload]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CURL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise DirectDownloadLinkException(
            "ERROR: Instagram API request timed out after "
            f"{CURL_TIMEOUT}s."
        )
    except FileNotFoundError:
        raise DirectDownloadLinkException(
            "ERROR: 'curl' is not installed on this system. "
            "Install curl to use Instagram downloader."
        )
    except Exception as e:
        raise DirectDownloadLinkException(
            f"ERROR: Instagram API request failed — {e}"
        )

    if result.returncode != 0:
        raise DirectDownloadLinkException(
            f"ERROR: Instagram API returned non-zero exit code: "
            f"{result.returncode}\n{result.stderr[:300]}"
        )

    if not result.stdout.strip():
        raise DirectDownloadLinkException(
            "ERROR: Instagram API returned empty response."
        )

    return _parse_fastdl_response(result.stdout, url)


def get_insta_direct_links(url: str) -> Dict:
    """
    Extract Instagram media and return a multi-file dict for direct download.

    Returns a dict compatible with add_direct_download():
    {"contents": [...], "title": str, "total_size": 0, "header": str}

    Each content item: {"path": str, "filename": str, "url": str}
    """
    data = extract_instagram_media(url)

    safe_title = _re.sub(r"[^\w.\- ]", "", data["title"].strip())[:80] or "Instagram_Media"
    folder = f"Instagram - {safe_title[:60]}"

    contents = []
    for i, link in enumerate(data["links"], 1):
        # Determine extension from URL
        ext = ".mp4"
        if ".jpg" in link.lower() or ".jpeg" in link.lower():
            ext = ".jpg"
        elif ".png" in link.lower():
            ext = ".png"
        elif ".webp" in link.lower():
            ext = ".webp"

        filename = f"{safe_title[:50]}_{i:02d}{ext}" if len(data["links"]) > 1 else f"{safe_title[:50]}{ext}"
        contents.append({
            "path": folder,
            "filename": filename,
            "url": link,
        })

    return {
        "contents": contents,
        "title": folder,
        "total_size": 0,
        "header": "",  # FastDL proxy handles headers
    }


def is_instagram_url(url: str) -> bool:
    """Check if URL is an Instagram link."""
    if not url:
        return False
    try:
        domain = urllib.parse.urlparse(url).hostname or ""
        return "instagram.com" in domain
    except Exception:
        return False
