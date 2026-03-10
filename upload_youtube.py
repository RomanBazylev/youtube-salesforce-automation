"""Upload generated Salesforce short to YouTube via Data API v3.

Required env vars (store as GitHub Secrets):
  YOUTUBE_CLIENT_ID      — OAuth2 client ID from Google Cloud Console
  YOUTUBE_CLIENT_SECRET  — OAuth2 client secret
  YOUTUBE_REFRESH_TOKEN  — refresh token obtained via one-time auth flow

Optional:
  YOUTUBE_PRIVACY        — public / unlisted / private (default: public)
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

BUILD_DIR = Path("build")
VIDEO_PATH = BUILD_DIR / "output_salesforce_short.mp4"
METADATA_PATH = BUILD_DIR / "metadata.json"

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

MAX_UPLOAD_RETRIES = 3


def _get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange refresh token for a short-lived access token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {resp.text}")
    return token


def _load_metadata() -> dict:
    """Load title/description/tags from metadata.json."""
    if METADATA_PATH.is_file():
        data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        return {
            "title": data.get("title", "Salesforce Tips #shorts")[:100],
            "description": data.get("description", "#salesforce #admin #shorts"),
            "tags": data.get("tags", ["salesforce", "admin", "shorts"]),
        }
    return {
        "title": "Salesforce Tips & Tricks #shorts",
        "description": "Watch till the end! #salesforce #admin #shorts",
        "tags": ["salesforce", "admin", "shorts"],
    }


def upload_video() -> str:
    """Upload video to YouTube. Returns video ID."""
    client_id = os.getenv("YOUTUBE_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET", "")
    refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        print("[SKIP] YouTube upload: missing credentials (YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN)")
        return ""

    if not VIDEO_PATH.is_file():
        print(f"[ERROR] Video not found: {VIDEO_PATH}")
        return ""

    privacy = os.getenv("YOUTUBE_PRIVACY", "public")
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"

    meta = _load_metadata()
    print(f"  Title: {meta['title']}")
    print(f"  Privacy: {privacy}")
    print(f"  Tags: {', '.join(meta['tags'])}")

    # Get access token
    print("  Obtaining access token...")
    access_token = _get_access_token(client_id, client_secret, refresh_token)

    # Build video resource metadata
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": "28",  # Science & Technology
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }

    print("  Initiating upload...")
    video_data = VIDEO_PATH.read_bytes()
    video_size = len(video_data)
    init_resp = requests.post(
        UPLOAD_URL,
        params={
            "uploadType": "resumable",
            "part": "snippet,status",
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(video_size),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=body,
        timeout=30,
    )
    if not init_resp.ok:
        print(f"[ERROR] Upload init failed ({init_resp.status_code}): {init_resp.text[:1000]}")
        init_resp.raise_for_status()
    upload_url = init_resp.headers["Location"]

    print(f"  Uploading {video_size / 1024 / 1024:.1f} MB...")
    for attempt in range(1, MAX_UPLOAD_RETRIES + 1):
        try:
            upload_resp = requests.put(
                upload_url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "video/mp4",
                    "Content-Length": str(video_size),
                },
                data=video_data,
                timeout=600,
            )
            upload_resp.raise_for_status()
            video_id = upload_resp.json().get("id", "")
            print(f"  Uploaded! https://youtube.com/shorts/{video_id}")
            return video_id
        except Exception as exc:
            print(f"[WARN] Upload attempt {attempt}/{MAX_UPLOAD_RETRIES} failed: {exc}")
            if attempt < MAX_UPLOAD_RETRIES:
                wait = attempt * 10
                print(f"  Retrying in {wait}s...")
                time.sleep(wait)

    print("[ERROR] All upload attempts failed.")
    return ""


if __name__ == "__main__":
    vid = upload_video()
    if not vid:
        print("Upload failed.")
        sys.exit(1)
