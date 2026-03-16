"""YouTube performance analytics — track video stats, weight topic selection.

Tracks uploads in performance_log.json, fetches YouTube stats via Data API,
and provides weighted topic selection based on historical performance.
Graceful fallback: if no data or API errors, returns None so callers use random.choice().
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

PERFORMANCE_LOG = Path("performance_log.json")
MAX_LOG_ENTRIES = 200
STATS_MAX_AGE_DAYS = 7

TOKEN_URL = "https://oauth2.googleapis.com/token"
VIDEOS_API = "https://www.googleapis.com/youtube/v3/videos"


# ── OAuth helpers (support both YT_ and YOUTUBE_ env prefixes) ─────

def _get_oauth_creds() -> tuple:
    client_id = os.getenv("YOUTUBE_CLIENT_ID") or os.getenv("YT_CLIENT_ID", "")
    client_secret = os.getenv("YOUTUBE_CLIENT_SECRET") or os.getenv("YT_CLIENT_SECRET", "")
    refresh_token = os.getenv("YOUTUBE_REFRESH_TOKEN") or os.getenv("YT_REFRESH_TOKEN", "")
    return client_id, client_secret, refresh_token


def _get_access_token() -> Optional[str]:
    client_id, client_secret, refresh_token = _get_oauth_creds()
    if not all([client_id, client_secret, refresh_token]):
        return None
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as exc:
        print(f"[ANALYTICS] Token error: {exc}")
        return None


# ── Log persistence ────────────────────────────────────────────────

def _load_log() -> dict:
    if PERFORMANCE_LOG.is_file():
        try:
            return json.loads(PERFORMANCE_LOG.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"videos": []}


def _save_log(data: dict) -> None:
    if len(data.get("videos", [])) > MAX_LOG_ENTRIES:
        data["videos"] = data["videos"][-MAX_LOG_ENTRIES:]
    PERFORMANCE_LOG.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── Upload logging ─────────────────────────────────────────────────

def log_upload(video_id: str, title: str, topic: str = "", tags: list = None) -> None:
    """Log a successful upload. Safe to call multiple times (deduplicates by video_id)."""
    if not video_id:
        return
    data = _load_log()
    existing_ids = {v.get("video_id") for v in data["videos"]}
    if video_id in existing_ids:
        return
    data["videos"].append({
        "video_id": video_id,
        "title": title,
        "topic": topic,
        "tags": tags or [],
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
        "stats": None,
    })
    _save_log(data)
    print(f"[ANALYTICS] Logged: {video_id} | {title[:50]}")


# ── Stats fetching ─────────────────────────────────────────────────

def fetch_and_update_stats() -> None:
    """Fetch YouTube stats for logged videos and update the log."""
    token = _get_access_token()
    if not token:
        print("[ANALYTICS] Stats fetch skipped — no valid credentials")
        return

    data = _load_log()
    videos = data.get("videos", [])
    if not videos:
        print("[ANALYTICS] No videos to check")
        return

    now = datetime.now(timezone.utc)
    ids_to_fetch = []
    for v in videos:
        try:
            uploaded = datetime.fromisoformat(v.get("uploaded_at", ""))
        except Exception:
            continue
        age_days = (now - uploaded).total_seconds() / 86400
        if age_days > STATS_MAX_AGE_DAYS and v.get("stats"):
            continue
        ids_to_fetch.append(v["video_id"])

    if not ids_to_fetch:
        print("[ANALYTICS] All stats up to date")
        return

    stats_map: Dict[str, dict] = {}
    for i in range(0, len(ids_to_fetch), 50):
        batch = ids_to_fetch[i:i + 50]
        try:
            resp = requests.get(VIDEOS_API, params={
                "part": "statistics",
                "id": ",".join(batch),
            }, headers={
                "Authorization": f"Bearer {token}",
            }, timeout=30)
            if resp.status_code == 403:
                print("[ANALYTICS] Stats fetch got 403 — scope may be insufficient")
                print("  Will retry next run. Consider adding youtube.readonly scope.")
                return
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                s = item.get("statistics", {})
                stats_map[item["id"]] = {
                    "views": int(s.get("viewCount", 0)),
                    "likes": int(s.get("likeCount", 0)),
                    "comments": int(s.get("commentCount", 0)),
                    "fetched_at": now.isoformat(),
                }
        except Exception as exc:
            print(f"[ANALYTICS] Stats fetch error: {exc}")
            return

    updated = 0
    for v in videos:
        if v["video_id"] in stats_map:
            v["stats"] = stats_map[v["video_id"]]
            updated += 1

    _save_log(data)
    print(f"[ANALYTICS] Updated stats for {updated}/{len(ids_to_fetch)} videos")


# ── Weighted topic selection ───────────────────────────────────────

def get_topic_weights(topics: List[str]) -> Optional[List[float]]:
    """Return weights for weighted random selection, or None if insufficient data."""
    data = _load_log()
    videos_with_stats = [
        v for v in data.get("videos", [])
        if v.get("stats") and v["stats"].get("views") is not None
    ]
    if len(videos_with_stats) < 3:
        return None

    all_views = [v["stats"]["views"] for v in videos_with_stats]
    avg_views = sum(all_views) / len(all_views) if all_views else 1

    topic_perf: Dict[str, List[int]] = {}
    for v in videos_with_stats:
        t = v.get("topic", "").lower().strip()
        if t:
            topic_perf.setdefault(t, []).append(v["stats"]["views"])

    if not topic_perf:
        return None

    weights = []
    for topic in topics:
        t_lower = topic.lower().strip()
        if t_lower in topic_perf:
            topic_avg = sum(topic_perf[t_lower]) / len(topic_perf[t_lower])
            if topic_avg > avg_views * 2:
                weights.append(3.0)
            elif topic_avg > avg_views:
                weights.append(2.0)
            elif topic_avg < avg_views * 0.3:
                weights.append(0.1)
            else:
                weights.append(1.0)
        else:
            weights.append(1.5)  # exploration bonus for untested topics

    return weights


# ── Report ─────────────────────────────────────────────────────────

def print_report() -> None:
    """Print formatted performance report to stdout."""
    data = _load_log()
    videos = data.get("videos", [])

    if not videos:
        print("[ANALYTICS] No videos logged yet")
        return

    videos_with_stats = [v for v in videos if v.get("stats")]
    total = len(videos)
    with_stats = len(videos_with_stats)

    print(f"\n{'=' * 60}")
    print(f"  ANALYTICS REPORT | {total} logged, {with_stats} with stats")
    print(f"{'=' * 60}")

    if not videos_with_stats:
        print("  No stats yet — will appear after next run.")
        print(f"{'=' * 60}\n")
        return

    sorted_vids = sorted(videos_with_stats, key=lambda v: v["stats"]["views"], reverse=True)
    all_views = [v["stats"]["views"] for v in sorted_vids]
    avg_views = sum(all_views) / len(all_views)

    print(f"  Total views: {sum(all_views):,} | Avg: {avg_views:,.0f}")
    print()

    print("  TOP performers:")
    for v in sorted_vids[:3]:
        s = v["stats"]
        print(f"    {s['views']:>6,} views | {s['likes']:>3} likes | {v['title'][:50]}")

    if len(sorted_vids) > 3:
        print("\n  BOTTOM performers:")
        for v in sorted_vids[-3:]:
            s = v["stats"]
            print(f"    {s['views']:>6,} views | {s['likes']:>3} likes | {v['title'][:50]}")

    topic_perf: Dict[str, List[int]] = {}
    for v in videos_with_stats:
        t = v.get("topic", "unknown")
        if t:
            topic_perf.setdefault(t, []).append(v["stats"]["views"])

    if topic_perf:
        print("\n  TOPIC performance:")
        topic_avgs = [(t, sum(vw) / len(vw), len(vw)) for t, vw in topic_perf.items()]
        topic_avgs.sort(key=lambda x: x[1], reverse=True)
        for t, avg, count in topic_avgs[:5]:
            flag = " +" if avg > avg_views * 1.5 else (" -" if avg < avg_views * 0.5 else "  ")
            print(f"   {flag} {avg:>6,.0f} avg ({count} vids) | {t[:40]}")

    print(f"{'=' * 60}\n")
