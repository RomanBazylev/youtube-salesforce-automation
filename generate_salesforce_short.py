import asyncio
import json
import os
import random
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import numpy as np
from PIL import Image
if not hasattr(Image, "ANTIALIAS"):
    Image.ANTIALIAS = Image.LANCZOS

import edge_tts
import requests
from moviepy.editor import (
    AudioFileClip,
    CompositeAudioClip,
    TextClip,
    VideoFileClip,
    CompositeVideoClip,
    concatenate_audioclips,
    concatenate_videoclips,
    vfx,
    afx,
)

# ── Constants ──────────────────────────────────────────────────────────
TARGET_W, TARGET_H = 1080, 1920
BUILD_DIR = Path("build")
CLIPS_DIR = BUILD_DIR / "clips"
AUDIO_DIR = BUILD_DIR / "audio_parts"
MUSIC_PATH = BUILD_DIR / "music.mp3"
HISTORY_PATH = BUILD_DIR / "topic_history.json"
MAX_HISTORY = 12  # remember last N topics to avoid repeats

# Voice rotation for variety
TTS_VOICES = [
    "en-US-GuyNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
]
TTS_RATE_OPTIONS = ["+5%", "+8%", "+10%"]

# Pronunciation fixes for Salesforce-specific terms
TTS_PRONUNCIATION_FIXES = {
    "SOQL": "sokel",
    "SOSL": "sosel",
    "Apex": "ay-pecks",
    "LWC": "L W C",
    "DML": "D M L",
    "SFDX": "S F D X",
    "CLI": "C L I",
    "ISV": "I S V",
    "CPQ": "C P Q",
    "API": "A P I",
    "JSON": "jay-son",
    "OAuth": "oh-auth",
    "CRUD": "crud",
    "FLS": "F L S",
    "SSO": "S S O",
    "MFA": "M F A",
    "DevOps": "dev-ops",
    "CI/CD": "C I C D",
    "Visualforce": "visual-force",
    "Trailhead": "trail-head",
}

# Content angles — how the topic is presented
ANGLES = [
    "mind-blowing trick that saves hours of work",
    "common mistake that even senior admins make",
    "hidden feature 90% of users don't know about",
    "quick automation that replaces manual work",
    "real-world scenario with step-by-step solution",
    "myth vs reality — what actually works",
    "beginner tip that pros still use daily",
    "config vs code — when to use which",
    "performance optimization that makes a huge difference",
    "certification prep tip that actually helps",
    "one setting that changes everything",
    "shortcut that makes you look like a wizard",
]

# Salesforce domains/products
SF_TOPICS = [
    "Flow Builder", "Apex triggers", "Lightning Web Components",
    "Reports & Dashboards", "Validation Rules", "Permission Sets",
    "Custom Metadata Types", "Platform Events", "SOQL queries",
    "Record-Triggered Flows", "Screen Flows", "Approval Processes",
    "Dynamic Forms", "Custom Objects", "Formula Fields",
    "Process Automation", "Data Loader", "Sandbox management",
    "Deployment", "User management", "Security settings",
    "Einstein AI", "Experience Cloud", "Service Cloud",
    "Sales Cloud", "Marketing Cloud", "Integration patterns",
]

# Audience level
SF_LEVELS = [
    "admin", "developer", "consultant", "architect",
    "beginner", "business analyst",
]

# Pexels fallback queries (tech/business visuals)
PEXELS_QUERIES = [
    "business technology",
    "computer coding",
    "office teamwork",
    "data dashboard",
    "cloud computing",
    "software development",
    "business meeting",
    "laptop work",
    "digital transformation",
    "tech workspace",
    "server room data center",
    "person typing keyboard",
    "startup office modern",
    "video conference call",
    "whiteboard planning",
    "mobile app business",
]


@dataclass
class ScriptPart:
    text: str


@dataclass
class VideoMetadata:
    title: str
    description: str
    tags: List[str]


# ── Topic deduplication ────────────────────────────────────────────────

def _load_topic_history() -> list:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_topic_history(history: list) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")


def _pick_unique_topic() -> str:
    """Pick a topic not recently used."""
    history = _load_topic_history()
    available = [t for t in SF_TOPICS if t not in history]
    if not available:
        available = SF_TOPICS
    topic = random.choice(available)
    history.append(topic)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    _save_topic_history(history)
    return topic


def _clean_build_dir() -> None:
    """Remove previous build artifacts to save disk space."""
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
        print("  Cleaned previous build directory")


def ensure_dirs() -> None:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)


FALLBACK_METADATA = VideoMetadata(
    title="5 Flow Builder Tricks Every Admin Needs 🚀 #shorts #salesforce",
    description=(
        "These Flow Builder tricks will save you hours every week. "
        "Which one is your favorite? Comment below!\n\n"
        "#salesforce #admin #flowbuilder #automation #shorts #trailblazer"
    ),
    tags=["salesforce", "admin", "flowbuilder", "automation", "shorts", "trailblazer", "crm"],
)

_CORE_TAGS = ["salesforce", "shorts", "admin", "crm", "trailblazer", "automation"]

_DESCRIPTION_FOOTER = (
    "\n\n#salesforce #admin #shorts #crm #trailblazer #automation"
    "\nFollow for daily Salesforce tips!"
)


def _enrich_metadata(meta: VideoMetadata) -> VideoMetadata:
    """Ensure title has #shorts, tags have core keywords, description has footer."""
    title = meta.title
    if "#shorts" not in title.lower():
        title = title.rstrip() + " #shorts"
    if "#salesforce" not in title.lower():
        title = title.rstrip() + " #salesforce"

    tags = list(meta.tags)
    for t in _CORE_TAGS:
        if t not in tags:
            tags.append(t)

    desc = meta.description
    if "#salesforce" not in desc.lower():
        desc = desc + _DESCRIPTION_FOOTER

    return VideoMetadata(title=title[:100], description=desc, tags=tags)


_FALLBACK_POOL = [
    [
        ScriptPart("Five Flow Builder tricks that every Salesforce admin needs to know right now."),
        ScriptPart("Number one — use Decision elements instead of multiple flows. One flow with branches runs faster than five separate ones."),
        ScriptPart("Number two — always add a Fault path. Without it, your flow fails silently and users see a generic error."),
        ScriptPart("Number three — use Custom Metadata Types for configuration values. Change them without deploying code."),
        ScriptPart("Number four — debug faster with the Flow Debug tool. Click Debug in the top right, set your input values, and trace every step."),
        ScriptPart("Number five — add entry conditions to Record-Triggered Flows. Without them, the flow runs on EVERY save, killing performance."),
        ScriptPart("A flow without entry conditions on an Account runs six hundred thousand times a year for just a thousand records. Add conditions."),
        ScriptPart("Bonus — name every element clearly. Future-you will thank present-you when debugging at two AM."),
        ScriptPart("Which trick was new to you? Drop a comment. Follow for daily Salesforce tips!"),
    ],
    [
        ScriptPart("Stop giving users Profile-level field access. Here's why Permission Sets are better."),
        ScriptPart("Profiles are rigid — one per user. Permission Sets are additive — stack as many as you need."),
        ScriptPart("Go to Setup, search Permission Sets, click New. Give it a clear name like 'Finance Field Access'."),
        ScriptPart("Add only the fields and objects this group needs. No more, no less."),
        ScriptPart("Now assign it to users. One user can have five Permission Sets — perfect for cross-functional roles."),
        ScriptPart("Need to remove access fast? Just unassign the Permission Set. No Profile cloning needed."),
        ScriptPart("Pro tip — use Permission Set Groups to bundle related sets. Assign one group instead of five sets."),
        ScriptPart("Audit time? Setup, search 'Permission Set Assignments' to see who has what."),
        ScriptPart("This approach scales from ten users to ten thousand. Start migrating from Profiles today."),
        ScriptPart("Save this for later. Follow for more Salesforce admin tips!"),
    ],
    [
        ScriptPart("Your SOQL queries are slow? Here are four fixes that actually work."),
        ScriptPart("Fix one — add selective filters. Queries without WHERE clauses scan every record in the table."),
        ScriptPart("Fix two — create custom indexes. Contact Salesforce Support to index your most-queried fields."),
        ScriptPart("Fix three — stop querying inside loops. Bulk your queries before the loop, then use a Map to look up records."),
        ScriptPart("Fix four — use SOQL FOR loops for large data sets. They process two hundred records at a time instead of loading everything into memory."),
        ScriptPart("Bonus — check Query Plan in Developer Console. It shows you exactly where the bottleneck is."),
        ScriptPart("One client cut their batch job from forty minutes to three minutes with these changes."),
        ScriptPart("Governor limits exist. Respect them or your code fails in production."),
        ScriptPart("Which fix will you try first? Comment below and follow for more Salesforce dev tips!"),
    ],
    [
        ScriptPart("Einstein AI inside Salesforce is more powerful than most admins realize."),
        ScriptPart("Einstein Lead Scoring ranks your leads automatically. No formulas, no manual rules."),
        ScriptPart("Go to Setup, search Einstein Lead Scoring, turn it on. It learns from your closed-won deals."),
        ScriptPart("Einstein Opportunity Insights predicts which deals will close and which are at risk."),
        ScriptPart("Einstein Activity Capture syncs emails and calendar events without users lifting a finger."),
        ScriptPart("Einstein Bots handle basic service requests. Create one in Setup under Einstein Bots."),
        ScriptPart("The best part? Most Einstein features are included in Enterprise Edition. No extra cost."),
        ScriptPart("One team increased conversion by twenty percent just by following Einstein's lead scores."),
        ScriptPart("Start with Lead Scoring — it takes ten minutes to enable and improves over time automatically."),
        ScriptPart("Save this video. Follow for more Salesforce AI tips!"),
    ],
]

_FALLBACK_META_POOL = [
    FALLBACK_METADATA,
    VideoMetadata(
        title="Permission Sets vs Profiles — Do It Right 🔒 #shorts #salesforce",
        description="Why Permission Sets beat Profiles every time. Stop cloning Profiles!\n\n#salesforce #admin #security #shorts",
        tags=["salesforce", "permission sets", "profiles", "admin", "security", "shorts"],
    ),
    VideoMetadata(
        title="4 SOQL Fixes That Actually Work ⚡ #shorts #salesforce",
        description="Your SOQL queries are slow? These 4 fixes cut query time dramatically.\n\n#salesforce #developer #soql #apex #shorts",
        tags=["salesforce", "soql", "apex", "developer", "performance", "shorts"],
    ),
    VideoMetadata(
        title="Einstein AI Features You're Not Using 🤖 #shorts #salesforce",
        description="Most Einstein features are already in your org. Here's how to turn them on.\n\n#salesforce #einstein #ai #shorts",
        tags=["salesforce", "einstein", "ai", "automation", "admin", "shorts"],
    ),
]


# Filler phrases that make content weak
_FILLER_PATTERNS = [
    "you won't believe", "this is amazing", "this is incredible", "let me tell you",
    "this changed everything", "trust me on this", "you need to hear this",
    "listen carefully", "here's the thing", "everyone should know",
    "let me explain", "this is so cool", "i was shocked", "you'll be surprised",
]


def _validate_script(parts: List[ScriptPart]) -> bool:
    """Quality gate — rejects weak/generic scripts."""
    if len(parts) < 8:
        print(f"[QUALITY] Rejected: too few parts ({len(parts)}, need >=8)")
        return False

    avg_words = sum(len(p.text.split()) for p in parts) / len(parts)
    if avg_words < 8:
        print(f"[QUALITY] Rejected: avg words too low ({avg_words:.1f}, need >=8)")
        return False

    # Check for filler phrases
    filler_count = 0
    for part in parts:
        text_lower = part.text.lower()
        for filler in _FILLER_PATTERNS:
            if filler in text_lower:
                filler_count += 1
                print(f"[QUALITY] Filler detected: '{part.text}'")
                break
    if filler_count > 2:
        print(f"[QUALITY] Rejected: too many fillers ({filler_count})")
        return False

    # At least 50% of phrases must contain Salesforce-specific or actionable content
    concrete_markers = re.compile(
        r'\d|flow|apex|soql|field|object|trigger|record|permission|'
        r'profile|role|report|dashboard|formula|validation|deploy|'
        r'sandbox|api|lightning|lwc|component|automat|query|'
        r'click|navigate|create|set up|configure|enable|go to|select|check',
        re.IGNORECASE,
    )
    concrete_count = sum(1 for p in parts if concrete_markers.search(p.text))
    ratio = concrete_count / len(parts)
    if ratio < 0.4:
        print(f"[QUALITY] Rejected: not enough concrete content ({ratio:.0%}, need >=40%)")
        return False

    print(f"[QUALITY] Passed: {len(parts)} parts, avg {avg_words:.1f} words, {ratio:.0%} concrete")
    return True


# ── Fallback script ────────────────────────────────────────────────────
def _fallback_script() -> tuple:
    idx = random.randrange(len(_FALLBACK_POOL))
    parts = _FALLBACK_POOL[idx]
    meta = _FALLBACK_META_POOL[idx]
    print(f"[FALLBACK] Using fallback script #{idx + 1}")
    return parts, meta


def call_groq_for_script() -> tuple:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return _fallback_script()

    angle = random.choice(ANGLES)
    topic = _pick_unique_topic()
    level = random.choice(SF_LEVELS)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    system_prompt = (
        "You are an experienced Salesforce professional and viral YouTube Shorts scriptwriter. "
        "You create scripts with REAL, ACTIONABLE Salesforce tips — specific clicks, settings, formulas, code snippets, or techniques. "
        "EVERY phrase must deliver CONCRETE value: a specific feature name, navigation path, number, or step-by-step action. "
        "NEVER write filler phrases like 'This is amazing' or 'You won't believe this' or 'Trust me on this'. "
        "Every phrase = a specific tip, fact, or action the viewer can use immediately. "
        "Write in a confident, conversational, professional tone — like a senior consultant sharing insider knowledge. "
        "Respond ONLY with valid JSON, no markdown wrappers or explanations."
    )

    user_prompt = f"""Write a YouTube Shorts script (45–60 seconds) about Salesforce.

CONTEXT:
- Topic: {topic}
- Angle: {angle}
- Target audience: Salesforce {level}

CONTENT REQUIREMENTS:
1. First phrase — powerful hook: a surprising stat, provocative question, or bold claim with a number.
2. EVERY phrase must contain SPECIFIC value: a feature name, menu path (Setup > X > Y), formula, code snippet, exact steps, or metric.
3. NO filler phrases. Banned: "This is amazing", "You won't believe", "Trust me", "This changed everything", "Let me explain".
4. Each phrase = 1–2 sentences, 12–25 words. Enough for substance, short enough for dynamics.
5. Use "you" — speak like a senior Salesforce pro advising a colleague.
6. Final phrase — call to action: ask which tip was best, ask to comment, follow for more.
7. 10–14 parts total (for 45–60 second video).
8. IMPORTANT: include real Salesforce terms, navigation paths, and specific examples — NOT generic business advice.

EXAMPLE OF GOOD PHRASE: "Go to Setup, search Permission Sets, and create one for each job function. Stop using Profiles for field access."
EXAMPLE OF BAD PHRASE: "This is a game changer!" or "You need to hear this."

Format — strictly JSON:
{{
  "title": "Catchy YouTube title (max 70 chars) with emoji and #shorts #salesforce",
  "description": "YouTube description (2–3 lines) with hashtags",
  "tags": ["salesforce", "admin", "shorts", ...4-7 more topic-specific tags],
  "pexels_queries": ["3–5 short English queries for Pexels video search, relevant to theme"],
  "parts": [
    {{ "text": "Phrase with specific actionable tip, 12-25 words" }}
  ]
}}"""

    print(f"  Topic: {topic} | Level: {level} | Angle: {angle}")

    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 2048,
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"[WARN] Groq API attempt 1 failed: {exc}, retrying...")
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=45)
            resp.raise_for_status()
        except Exception as exc2:
            print(f"[WARN] Groq API attempt 2 failed: {exc2}, using fallback")
            return _fallback_script()

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$", "", content.strip())
        data = json.loads(content)
        parts = [ScriptPart(p["text"]) for p in data.get("parts", []) if p.get("text")]
        metadata = VideoMetadata(
            title=data.get("title", "")[:100] or "Salesforce Tips & Tricks #shorts",
            description=data.get("description", "") or "Watch till the end! #salesforce #admin #shorts",
            tags=data.get("tags", ["salesforce", "admin", "shorts"]),
        )
        metadata = _enrich_metadata(metadata)
        llm_queries = data.get("pexels_queries", [])
        if llm_queries:
            global _llm_pexels_queries
            _llm_pexels_queries = [q for q in llm_queries if isinstance(q, str)][:5]

        if _validate_script(parts):
            return parts, metadata
        print("[WARN] LLM output failed quality check, retrying...")
    except Exception as exc:
        print(f"[WARN] Groq parse error: {exc}, retrying...")

    # ── Retry with reinforced prompt ──
    body["messages"].append({
        "role": "user",
        "content": (
            "IMPORTANT: the previous response failed quality checks. "
            "Make sure:\n"
            "1. At least 10 parts, each 12-25 words.\n"
            "2. Every part has SPECIFIC Salesforce content: feature names, navigation paths, code, numbers.\n"
            "3. NO filler phrases.\n"
            "Return JSON in the same format."
        ),
    })
    body["temperature"] = 1.0
    try:
        resp2 = requests.post(url, headers=headers, json=body, timeout=45)
        resp2.raise_for_status()
        content2 = resp2.json()["choices"][0]["message"]["content"]
        content2 = re.sub(r"^```(?:json)?\s*", "", content2.strip())
        content2 = re.sub(r"\s*```$", "", content2.strip())
        data2 = json.loads(content2)
        parts2 = [ScriptPart(p["text"]) for p in data2.get("parts", []) if p.get("text")]
        metadata2 = VideoMetadata(
            title=data2.get("title", "")[:100] or "Salesforce Tips & Tricks #shorts",
            description=data2.get("description", "") or "Watch till the end! #salesforce #admin #shorts",
            tags=data2.get("tags", ["salesforce", "admin", "shorts"]),
        )
        metadata2 = _enrich_metadata(metadata2)
        llm_queries2 = data2.get("pexels_queries", [])
        if llm_queries2:
            _llm_pexels_queries = [q for q in llm_queries2 if isinstance(q, str)][:5]
        if _validate_script(parts2):
            return parts2, metadata2
        print("[WARN] Retry also failed quality check, using fallback")
    except Exception as exc:
        print(f"[WARN] Retry failed: {exc}, using fallback")

    return _fallback_script()


# Global for LLM-generated Pexels queries
_llm_pexels_queries: List[str] = []


# ── Download clips ─────────────────────────────────────────────────────
def _download_file(url: str, dest: Path) -> None:
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with dest.open("wb") as f:
        for chunk in r.iter_content(chunk_size=32768):
            if chunk:
                f.write(chunk)


def _pexels_best_file(video_files: list) -> Optional[dict]:
    """Pick the best HD file from Pexels video_files list."""
    hd = [f for f in video_files if (f.get("height") or 0) >= 720]
    if hd:
        return min(hd, key=lambda f: abs((f.get("height") or 0) - 1920))
    if video_files:
        return max(video_files, key=lambda f: f.get("height") or 0)
    return None


def download_pexels_clips(target_count: int = 14) -> List[Path]:
    """Download clips using LLM-generated + fallback queries for visual diversity."""
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        return []

    headers = {"Authorization": api_key}
    all_queries = list(_llm_pexels_queries)
    extra = [q for q in PEXELS_QUERIES if q not in all_queries]
    random.shuffle(extra)
    all_queries.extend(extra)
    queries = all_queries[:target_count]
    result_paths: List[Path] = []
    seen_ids: set = set()
    clip_idx = 0

    for query in queries:
        if len(result_paths) >= target_count:
            break
        params = {
            "query": query,
            "per_page": 3,
            "orientation": "portrait",
        }
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers, params=params, timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"[WARN] Pexels search '{query}' failed: {exc}")
            continue

        for video in resp.json().get("videos", []):
            vid_id = video.get("id")
            if vid_id in seen_ids:
                continue
            seen_ids.add(vid_id)
            best = _pexels_best_file(video.get("video_files", []))
            if not best:
                continue
            clip_idx += 1
            clip_path = CLIPS_DIR / f"pexels_{clip_idx}.mp4"
            try:
                _download_file(best["link"], clip_path)
                result_paths.append(clip_path)
                print(f"    Pexels [{query}] -> clip {clip_idx}")
            except Exception as exc:
                print(f"[WARN] Pexels clip {clip_idx} download failed: {exc}")
            if len(result_paths) >= target_count:
                break

    return result_paths


def download_pixabay_clips(max_clips: int = 3) -> List[Path]:
    api_key = os.getenv("PIXABAY_API_KEY")
    if not api_key:
        return []

    params = {
        "key": api_key,
        "q": random.choice(_llm_pexels_queries or ["technology", "computer work", "business"]),
        "per_page": max_clips,
        "safesearch": "true",
        "order": "popular",
    }

    try:
        resp = requests.get(
            "https://pixabay.com/api/videos/",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[WARN] Pixabay API error: {exc}")
        return []

    data = resp.json()
    result_paths: List[Path] = []

    for idx, hit in enumerate(data.get("hits", [])[:max_clips], start=1):
        videos = hit.get("videos") or {}
        cand = videos.get("large") or videos.get("medium") or videos.get("small")
        if not cand or "url" not in cand:
            continue
        url = cand["url"]
        clip_path = CLIPS_DIR / f"pixabay_{idx}.mp4"
        try:
            _download_file(url, clip_path)
            result_paths.append(clip_path)
        except Exception as exc:
            print(f"[WARN] Failed to download Pixabay clip {idx}: {exc}")

    return result_paths


def download_background_music() -> Optional[Path]:
    if os.getenv("DISABLE_BG_MUSIC") == "1":
        return None

    if MUSIC_PATH.is_file():
        return MUSIC_PATH

    candidate_urls = [
        "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Komiku/Its_time_for_adventure/Komiku_-_05_-_Friends.mp3",
        "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Podington_Bear/Daydream/Podington_Bear_-_Daydream.mp3",
        "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/ccCommunity/Chad_Crouch/Arps/Chad_Crouch_-_Shipping_Lanes.mp3",
        "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Lobo_Loco/Folkish_things/Lobo_Loco_-_01_-_Acoustic_Dreams_ID_1199.mp3",
    ]

    for url in random.sample(candidate_urls, len(candidate_urls)):
        try:
            _download_file(url, MUSIC_PATH)
            return MUSIC_PATH
        except Exception:
            continue
    return None


# ── TTS (edge-tts, per-phrase) ─────────────────────────────────────────
def _fix_pronunciation(text: str) -> str:
    """Replace hard-to-pronounce terms with phonetic equivalents."""
    result = text
    for word, replacement in TTS_PRONUNCIATION_FIXES.items():
        result = re.sub(re.escape(word), replacement, result, flags=re.IGNORECASE)
    return result


async def _generate_all_audio(parts: List[ScriptPart]) -> List[Path]:
    """Generate all audio phrases in parallel."""
    voice = random.choice(TTS_VOICES)
    rate = random.choice(TTS_RATE_OPTIONS)
    print(f"  TTS voice: {voice}, rate: {rate}")
    audio_paths: List[Path] = []
    tasks = []
    for i, part in enumerate(parts):
        out = AUDIO_DIR / f"part_{i}.mp3"
        audio_paths.append(out)
        tts_text = _fix_pronunciation(part.text)
        comm = edge_tts.Communicate(tts_text, voice, rate=rate)
        tasks.append(comm.save(str(out)))
    await asyncio.gather(*tasks)
    return audio_paths


def build_tts_per_part(parts: List[ScriptPart]) -> List[Path]:
    """Generate a separate mp3 for each phrase — perfect sync."""
    return asyncio.run(_generate_all_audio(parts))


# ── Video assembly ─────────────────────────────────────────────────────
def _fit_clip_to_frame(clip: VideoFileClip, duration: float) -> VideoFileClip:
    """Trim/loop clip to duration, crop to 9:16."""
    if clip.duration > duration + 0.5:
        max_start = clip.duration - duration
        start = random.uniform(0, max_start)
        segment = clip.subclip(start, start + duration)
    else:
        segment = clip.fx(vfx.loop, duration=duration)

    margin = 1.10
    src_ratio = segment.w / segment.h
    target_ratio = TARGET_W / TARGET_H
    if src_ratio > target_ratio:
        segment = segment.resize(height=int(TARGET_H * margin))
    else:
        segment = segment.resize(width=int(TARGET_W * margin))

    segment = segment.crop(
        x_center=segment.w / 2, y_center=segment.h / 2,
        width=TARGET_W, height=TARGET_H,
    )
    return segment


def _apply_ken_burns(clip, duration: float):
    """Slow zoom-in or zoom-out for visual dynamics."""
    direction = random.choice(["in", "out"])
    start_scale = 1.0
    end_scale = random.uniform(1.06, 1.12)
    if direction == "out":
        start_scale, end_scale = end_scale, start_scale

    def make_frame(get_frame, t):
        progress = t / max(duration, 0.01)
        scale = start_scale + (end_scale - start_scale) * progress
        frame = get_frame(t)
        h, w = frame.shape[:2]
        new_h, new_w = int(h * scale), int(w * scale)
        img = Image.fromarray(frame)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        arr = np.array(img)
        y_off = (new_h - h) // 2
        x_off = (new_w - w) // 2
        return arr[y_off:y_off + h, x_off:x_off + w]

    return clip.fl(make_frame)


def _make_subtitle(text: str, duration: float) -> list:
    """Subtitle with stroke — readable on any background."""
    shadow = (
        TextClip(
            text,
            fontsize=72,
            color="black",
            font="DejaVu-Sans-Bold",
            method="caption",
            size=(TARGET_W - 80, None),
            stroke_color="black",
            stroke_width=5,
        )
        .set_position(("center", 0.70), relative=True)
        .set_duration(duration)
    )
    main_txt = (
        TextClip(
            text,
            fontsize=72,
            color="white",
            font="DejaVu-Sans-Bold",
            method="caption",
            size=(TARGET_W - 80, None),
            stroke_color="black",
            stroke_width=3,
        )
        .set_position(("center", 0.70), relative=True)
        .set_duration(duration)
    )
    return [shadow, main_txt]


def build_video(
    parts: List[ScriptPart],
    clip_paths: List[Path],
    audio_parts: List[Path],
    music_path: Optional[Path],
) -> Path:
    if not clip_paths:
        raise RuntimeError("No video clips downloaded. Provide PEXELS_API_KEY or PIXABAY_API_KEY.")

    part_audios = [AudioFileClip(str(p)) for p in audio_parts]
    durations = [a.duration for a in part_audios]
    total_duration = sum(durations)

    voice = concatenate_audioclips(part_audios)

    if len(clip_paths) >= len(parts):
        chosen_clips = random.sample(clip_paths, len(parts))
    else:
        chosen_clips = clip_paths[:]
        random.shuffle(chosen_clips)
        while len(chosen_clips) < len(parts):
            chosen_clips.append(random.choice(clip_paths))

    source_clips = []
    video_clips = []
    for i, part in enumerate(parts):
        src_path = chosen_clips[i]
        clip = VideoFileClip(str(src_path))
        source_clips.append(clip)
        dur = durations[i]

        fitted = _fit_clip_to_frame(clip, dur)
        fitted = _apply_ken_burns(fitted, dur)

        subtitle_layers = _make_subtitle(part.text, dur)

        composed = CompositeVideoClip(
            [fitted] + subtitle_layers,
            size=(TARGET_W, TARGET_H),
        ).set_duration(dur)
        video_clips.append(composed)

    FADE_DUR = 0.2
    for idx in range(1, len(video_clips)):
        video_clips[idx] = video_clips[idx].crossfadein(FADE_DUR)

    video = concatenate_videoclips(video_clips, method="compose").set_duration(total_duration)

    audio_tracks = [voice]
    bg = None
    if music_path and music_path.is_file():
        bg = AudioFileClip(str(music_path)).volumex(0.10)
        bg = bg.set_duration(total_duration)
        bg = bg.fx(afx.audio_fadeout, min(1.5, total_duration * 0.1))
        audio_tracks.append(bg)

    final_audio = CompositeAudioClip(audio_tracks)
    video = video.set_audio(final_audio).set_duration(total_duration)

    output_path = BUILD_DIR / "output_salesforce_short.mp4"
    video.write_videofile(
        str(output_path),
        fps=30,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        bitrate="8000k",
        threads=4,
    )

    voice.close()
    if bg is not None:
        bg.close()
    for a in part_audios:
        a.close()
    for vc in video_clips:
        vc.close()
    for sc in source_clips:
        sc.close()
    video.close()

    return output_path


def _save_metadata(meta: VideoMetadata) -> None:
    """Save video metadata to JSON for auto-upload."""
    meta_path = BUILD_DIR / "metadata.json"
    meta_path.write_text(
        json.dumps(
            {"title": meta.title, "description": meta.description, "tags": meta.tags},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  Metadata saved to {meta_path}")


def main() -> None:
    _clean_build_dir()
    ensure_dirs()
    print("[1/5] Generating script...")
    parts, metadata = call_groq_for_script()
    print(f"  Script: {len(parts)} parts")
    print(f"  Title: {metadata.title}")
    total_words = 0
    for i, p in enumerate(parts, 1):
        wc = len(p.text.split())
        total_words += wc
        print(f"  [{i}] ({wc}w) {p.text}")
    est_duration = total_words / 2.8  # ~2.8 words/sec for English TTS
    print(f"  Estimated duration: ~{est_duration:.0f}s ({total_words} words)")
    _save_metadata(metadata)

    print("[2/5] Downloading video clips...")
    clip_paths = download_pexels_clips()
    clip_paths += download_pixabay_clips()
    print(f"  Downloaded {len(clip_paths)} clips")

    print("[3/5] Generating TTS audio (edge-tts, per-part)...")
    audio_parts = build_tts_per_part(parts)
    for i, ap in enumerate(audio_parts):
        a = AudioFileClip(str(ap))
        print(f"  Part {i+1}: {a.duration:.1f}s")
        a.close()

    print("[4/5] Downloading background music...")
    music_path = download_background_music()

    print("[5/5] Building final video...")
    output = build_video(parts, clip_paths, audio_parts, music_path)
    print(f"Done! Video saved to: {output}")


if __name__ == "__main__":
    main()
