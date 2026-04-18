"""
Long-form Salesforce video generator (8–12 min, landscape 16:9).

Pipeline: fetch fresh content → scrape full article → extract facts (LLM) →
          generate script (LLM) → edge-tts with WordBoundary → Pexels clips →
          ffmpeg assembly (subtitles + ducked music) → upload to YouTube
"""

import asyncio
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import urllib3.util.connection

import edge_tts
import requests

# Force IPv4 — GitHub Actions runners often lack IPv6 connectivity
urllib3.util.connection.HAS_IPV6 = False

# ── Constants ──────────────────────────────────────────────────────────
BUILD_DIR = Path("build")
CLIPS_DIR = BUILD_DIR / "clips"
AUDIO_PATH = BUILD_DIR / "voiceover.mp3"
MUSIC_PATH = BUILD_DIR / "music.mp3"
METADATA_PATH = BUILD_DIR / "metadata.json"
OUTPUT_PATH = BUILD_DIR / "output_salesforce_long.mp4"

TARGET_W, TARGET_H = 1280, 720
FPS = 30
FFMPEG_PRESET = "medium"
FFMPEG_CRF = "23"

# Voice rotation for variety
TTS_VOICES = [
    "en-US-GuyNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
]
TTS_RATE = "+3%"

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_FALLBACK_MODEL = "llama-3.1-8b-instant"

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

PEXELS_QUERIES = [
    "computer screen code dark",
    "programmer typing laptop closeup",
    "software dashboard interface screen",
    "server room lights rack",
    "database code terminal dark",
    "developer coding screen IDE",
    "digital data visualization graph",
    "tech workspace dual monitors keyboard",
    "cybersecurity network lock",
    "cloud infrastructure server rack",
    "dark mode code editor syntax",
    "system admin terminal linux",
    "technology abstract data flow",
    "programming code closeup python",
    "cloud computing data center",
    "network cables server rack blue",
    "code deployment pipeline CI CD",
    "API integration code screen",
    "DevOps monitoring dashboard grafana",
    "kubernetes container orchestration",
    "SaaS application interface dark",
    "data analytics chart screen",
    "circuit board technology macro",
    "typing keyboard code neon",
    "binary code digital abstract",
    "cloud server blue technology",
    "software testing automation screen",
    "machine learning neural network",
]

MUSIC_URLS = [
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Komiku/Its_time_for_adventure/Komiku_-_05_-_Friends.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Podington_Bear/Daydream/Podington_Bear_-_Daydream.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/ccCommunity/Chad_Crouch/Arps/Chad_Crouch_-_Shipping_Lanes.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Lobo_Loco/Folkish_things/Lobo_Loco_-_01_-_Acoustic_Dreams_ID_1199.mp3",
]

_CORE_TAGS = ["salesforce", "admin", "crm", "trailblazer", "automation", "tutorial"]

_DESCRIPTION_FOOTER = (
    "\n\n---\n"
    "#salesforce #admin #crm #trailblazer #automation #tutorial\n"
    "Subscribe for weekly Salesforce deep-dives! 🔔\n"
    "Drop your questions in the comments 👇"
)

# ── Fallback scripts for when all live sources fail ────────────────────

_FALLBACK_FACTS = [
    (
        "Flow Builder Advanced Patterns",
        "1. Subflows improve reusability — create one subflow for address validation, call it from 10+ flows.\n"
        "2. Collection variables in flows hold multiple records — loop through them for bulk operations.\n"
        "3. Action elements in screen flows let you call Apex without writing full triggers.\n"
        "4. Scheduled-triggered flows replace many Process Builder time-based actions.\n"
        "5. Flow fault handling with Fault connectors prevents silent failures.\n"
        "6. Custom error messages in validation rules vs flow Fault paths: flows give more control.\n"
        "7. Debug mode in Flow Builder lets you trace every decision path with test data.\n"
        "8. Flow performance: avoid DML inside loops — collect records and do one DML outside.\n"
        "9. Entry conditions reduce flow executions by 80%+ on high-volume objects.\n"
        "10. Auto-layout vs free-form: auto-layout enforces clean design for complex flows.",
    ),
    (
        "Salesforce Security Best Practices 2026",
        "1. Permission Set Groups replace profile-based access — assign bundles of permissions.\n"
        "2. Transaction Security policies detect anomalous API usage in real-time.\n"
        "3. Shield Platform Encryption encrypts data at rest — fields, files, attachments.\n"
        "4. Event Monitoring tracks 50+ event types — login, API, report export, Apex execution.\n"
        "5. MFA is mandatory since February 2022 — enforce it for all internal users.\n"
        "6. Custom domains eliminate instance-specific URLs — reduce phishing risk.\n"
        "7. Health Check in Setup scores your security settings against Salesforce baseline.\n"
        "8. Session security settings: lock to IP range, set timeouts, require HTTPS.\n"
        "9. Named Credentials store external system auth securely — never hardcode tokens.\n"
        "10. Org-wide defaults + sharing rules + permission sets = defense in depth.",
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────
def _clean_build_dir():
    if BUILD_DIR.is_dir():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)


def _run_ffmpeg(cmd: list):
    print(f"[CMD] {' '.join(cmd[:8])}... ({len(cmd)} args)")
    subprocess.run(cmd, check=True)


def _probe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        text=True,
    ).strip()
    return float(out)


def _fix_pronunciation(text: str) -> str:
    result = text
    for word, replacement in TTS_PRONUNCIATION_FIXES.items():
        result = re.sub(re.escape(word), replacement, result, flags=re.IGNORECASE)
    return result


def _groq_call(messages: list, temperature: float = 0.7,
               max_tokens: int = 4096, json_mode: bool = False) -> Optional[str]:
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for model in [GROQ_MODEL, GROQ_FALLBACK_MODEL]:
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        max_attempts = 8 if model == GROQ_MODEL else 5
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(GROQ_URL, headers=headers, json=body, timeout=90)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError:
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    if retry_after:
                        wait = min(int(float(retry_after)) + 2, 300)
                    else:
                        wait = min(15 * (2 ** (attempt - 1)), 300)
                    print(f"[WARN] Groq {model} attempt {attempt}: 429 rate limited, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[WARN] Groq {model} attempt {attempt}: HTTP {r.status_code}")
                    time.sleep(5)
            except Exception as exc:
                print(f"[WARN] Groq {model} attempt {attempt}: {exc}")
                time.sleep(5)
        print(f"[WARN] Groq {model} exhausted {max_attempts} attempts, trying next model...")
    return None


# ── Content Sourcing ──────────────────────────────────────────────────

def _get_fresh_content():
    """Fetch a fresh content item from live sources."""
    try:
        from sf_content_sources import pick_fresh_content, scrape_full_article
        item = pick_fresh_content("long")
        if item:
            # Ensure we have full text for long-form
            item = scrape_full_article(item)
            return item
    except Exception as exc:
        print(f"[WARN] Live content fetch failed: {exc}")
    return None


# ── Two-Step LLM Pipeline ────────────────────────────────────────────

def step1_extract_facts(article_title: str, article_text: str) -> Optional[str]:
    """Step 1: Compress article into 7–10 key facts (~500 words)."""
    words = article_text.split()
    if len(words) > 8000:
        article_text = " ".join(words[:8000])

    messages = [
        {"role": "system", "content": (
            "You are a senior Salesforce consultant with 10+ years of experience. "
            "Your task is to extract 7-10 KEY facts from the article below. "
            "Write ONLY the facts — no introductions, no filler. "
            "Each fact = 1-2 sentences. Preserve specifics: feature names, paths, "
            "code snippets, numbers, version info, step-by-step instructions. "
            "Total output ~500 words."
        )},
        {"role": "user", "content": (
            f"Article title: {article_title}\n\n"
            f"Article text:\n{article_text}\n\n"
            "Extract 7-10 key facts from this article."
        )},
    ]
    result = _groq_call(messages, temperature=0.3, max_tokens=2048)
    if result:
        print(f"[STEP1] Extracted facts: {len(result.split())} words")
    return result


def step2_generate_script(facts: str, article_title: str) -> Optional[dict]:
    """Step 2: Generate a YouTube-ready script from extracted facts."""
    messages = [
        {"role": "system", "content": (
            "You are a popular Salesforce YouTuber with a channel called 'Salesforce Pro Tips'. "
            "You make 8-12 minute deep-dive videos that are both educational and engaging. "
            "Style: like explaining to a colleague over coffee — confident, clear, practical.\n\n"
            "RULES:\n"
            "- Each sentence: maximum 15 words (for TTS narration).\n"
            "- Use transition phrases: 'Now here's where it gets interesting', "
            "'Let me show you something powerful', 'One more thing you need to know', "
            "'This is the part most people miss'.\n"
            "- Include SPECIFIC Salesforce terms, navigation paths, code references.\n"
            "- DO NOT copy the source text — rewrite in YOUR voice.\n"
            "- Structure: Hook → 5-7 deep sections → Summary → CTA.\n\n"
            "Respond ONLY with valid JSON."
        )},
        {"role": "user", "content": f"""Write a YouTube video script (8-12 minutes) about Salesforce.

TOPIC: {article_title}

KEY FACTS:
{facts}

CRITICAL: The "script" field MUST contain AT LEAST 1200 words.
This is a LONG-FORM video, not a short. If the script is under 800 words — the video cannot be produced.

STRUCTURE (write ALL sections FULLY, do NOT skip):
1. HOOK (60–80 words): Start with a bold claim, surprising stat, or provocative question. Promise concrete value.
2. MAIN CONTENT (5–7 blocks, each 150–200 words):
   - Each block starts with a transition phrase
   - Deep explanation: WHAT it is, WHY it matters, HOW to set it up
   - Concrete example: specific navigation path, code snippet, or real-world scenario
   - Mini-conclusion for the block
3. SUMMARY (60–80 words): 3 key takeaways from the video.
4. CTA (30–40 words): Ask to subscribe, like, comment with questions.

WORD CALCULATION: Hook (~70) + 6 blocks × 175 words (~1050) + Summary (~70) + CTA (~35) = ~1225 words.
You MUST write at least 1200 words. Count carefully.

JSON FORMAT:
{{
  "title": "Video title, max 90 characters, with emoji",
  "description": "YouTube description, 5-8 lines with hashtags",
  "tags": ["salesforce", "admin", ...15+ relevant tags],
  "pexels_queries": ["5-8 English queries for background footage"],
  "script": "ONE STRING (not array) with 1200-1800 words. Sentences separated by newlines."
}}"""},
    ]
    content = _groq_call(messages, temperature=0.8, max_tokens=16384, json_mode=True)
    if not content:
        return None
    try:
        content = re.sub(r"^```(?:json)?\s*", "", content.strip())
        content = re.sub(r"\s*```$", "", content.strip())
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            content = content[start:end + 1]
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            content = re.sub(r'[\x00-\x1f\x7f]', lambda m: f'\\u{ord(m.group()):04x}', content)
            data = json.loads(content)
        script = data.get("script", "")
        if isinstance(script, list):
            script = "\n".join(str(s) for s in script)
            data["script"] = script
        word_count = len(script.split())
        print(f"[STEP2] Script generated: {word_count} words")
        if word_count < 250:
            print("[WARN] Script too short (< 250 words), skipping")
            return None
        if word_count < 600:
            print(f"[WARN] Script shorter than ideal ({word_count} words), but usable")
        return data
    except Exception as exc:
        print(f"[WARN] JSON parse failed: {exc}")
        return None


def _fallback_long_script() -> tuple:
    """Emergency fallback — use pre-written facts to generate script."""
    idx = random.randrange(len(_FALLBACK_FACTS))
    title, facts = _FALLBACK_FACTS[idx]
    print(f"[FALLBACK] Using fallback facts: {title}")
    return title, facts


# ── TTS ───────────────────────────────────────────────────────────────

async def _generate_tts(text: str, output_path: Path) -> list:
    voice = random.choice(TTS_VOICES)
    tts_text = _fix_pronunciation(text)
    comm = edge_tts.Communicate(tts_text, voice, rate=TTS_RATE)
    word_events = []
    with open(output_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                word_events.append({
                    "text": chunk["text"],
                    "offset": chunk["offset"] / 10_000_000,
                    "duration": chunk["duration"] / 10_000_000,
                })
    print(f"[TTS] {voice}, {len(word_events)} words, file={output_path}")
    return word_events


def generate_tts(text: str) -> tuple:
    word_events = asyncio.run(_generate_tts(text, AUDIO_PATH))
    return AUDIO_PATH, word_events


# ── Clip Downloading ─────────────────────────────────────────────────

def _download_file(url: str, dest: Path):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with dest.open("wb") as f:
        for chunk in r.iter_content(32768):
            if chunk:
                f.write(chunk)


def download_clips(extra_queries: list = None, target: int = 35) -> list:
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        print("[WARN] No PEXELS_API_KEY")
        return []

    queries = list(extra_queries or [])
    base = [q for q in PEXELS_QUERIES if q not in queries]
    random.shuffle(base)
    queries.extend(base)

    headers = {"Authorization": api_key}
    paths = []
    seen_ids = set()
    idx = 0

    for query in queries:
        if len(paths) >= target:
            break
        try:
            resp = requests.get(
                "https://api.pexels.com/videos/search",
                headers=headers,
                params={"query": query, "per_page": 3, "orientation": "landscape"},
                timeout=30,
            )
            resp.raise_for_status()
        except Exception as exc:
            print(f"[WARN] Pexels '{query}': {exc}")
            continue

        for video in resp.json().get("videos", []):
            vid_id = video.get("id")
            if vid_id in seen_ids:
                continue
            seen_ids.add(vid_id)
            hd = [f for f in video.get("video_files", []) if (f.get("height") or 0) >= 720]
            if not hd:
                continue
            best = min(hd, key=lambda f: abs((f.get("height") or 0) - 720))
            idx += 1
            clip_path = CLIPS_DIR / f"clip_{idx:03d}.mp4"
            try:
                _download_file(best["link"], clip_path)
                paths.append(clip_path)
            except Exception:
                pass
            if len(paths) >= target:
                break

    print(f"[CLIPS] Downloaded {len(paths)} clips")
    return paths


def download_music() -> Optional[Path]:
    for url in random.sample(MUSIC_URLS, len(MUSIC_URLS)):
        try:
            _download_file(url, MUSIC_PATH)
            return MUSIC_PATH
        except Exception:
            continue
    return None


# ── FFmpeg Assembly ──────────────────────────────────────────────────

def _prepare_clip(src: Path, dst: Path, duration: int = 5):
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},fps={FPS}"
    )
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", str(src), "-t", str(duration),
        "-vf", vf, "-an", "-c:v", "libx264",
        "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF, str(dst),
    ])


def _fmt_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(seconds * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _safe_text(raw: str) -> str:
    text = raw.replace("\\", " ").replace("\n", " ")
    text = text.replace(":", " ").replace(";", " ")
    text = text.replace("'", "").replace('"', "")
    text = re.sub(r"\s+", " ", text).strip()
    return text or " "


def _group_words(word_events: list, max_per_line: int = 6) -> list:
    if not word_events:
        return []
    lines = []
    buf_words, buf_start, buf_end, buf_kara = [], 0.0, 0.0, []
    for ev in word_events:
        start, dur = ev["offset"], ev["duration"]
        end = start + dur
        if buf_words and (len(buf_words) >= max_per_line or (start - buf_end) > 0.6):
            lines.append({"start": buf_start, "end": buf_end,
                          "text": " ".join(buf_words), "words": list(buf_kara)})
            buf_words, buf_kara = [], []
        if not buf_words:
            buf_start = start
        buf_words.append(ev["text"])
        buf_kara.append({"text": ev["text"], "offset": start, "duration": dur})
        buf_end = end
    if buf_words:
        lines.append({"start": buf_start, "end": buf_end,
                      "text": " ".join(buf_words), "words": list(buf_kara)})
    return lines


def _write_ass(word_events: list, ass_path: Path) -> Path:
    font_size = 42
    margin_v = 80
    primary = "&H0000D4FF"     # Yellow-orange (spoken)
    secondary = "&H00FFFFFF"   # White (upcoming)
    outline = "&H00000000"
    shadow = "&H80000000"

    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        f"PlayResX: {TARGET_W}\nPlayResY: {TARGET_H}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Kara,DejaVu Sans,{font_size},{primary},{secondary},{outline},{shadow},"
        f"1,0,0,0,100,100,1,0,1,3,2,2,30,30,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = _group_words(word_events)
    events = []
    for line in lines:
        start = line["start"]
        end = line["end"] + 0.15
        parts = []
        for w in line["words"]:
            dur_cs = max(5, int(w["duration"] * 100))
            safe = _safe_text(w["text"]).upper()
            parts.append(f"{{\\kf{dur_cs}}}{safe}")
        kara_text = " ".join(parts)
        events.append(
            f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},Kara,,0,0,0,,{kara_text}"
        )

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    print(f"[SUBS] {len(events)} lines, {len(word_events)} words → {ass_path}")
    return ass_path


def assemble_video(
    clips: list,
    voiceover: Path,
    word_events: list,
    music: Optional[Path],
) -> Path:
    temp = BUILD_DIR / "temp"
    temp.mkdir(exist_ok=True)

    # Prepare clips (5 sec each, landscape)
    prepared = []
    for i, clip in enumerate(clips):
        dst = temp / f"prep_{i:03d}.mp4"
        _prepare_clip(clip, dst, duration=5)
        prepared.append(dst)

    # Concatenate all clips
    concat_file = temp / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in prepared),
        encoding="utf-8",
    )
    silent = temp / "silent.mp4"
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat_file), "-c", "copy", str(silent)])

    voice_dur = _probe_duration(voiceover)
    clip_dur = _probe_duration(silent)
    final_dur = voice_dur + 1.5

    # Loop video if shorter than voiceover
    if clip_dur < voice_dur:
        looped = temp / "looped.mp4"
        _run_ffmpeg([
            "ffmpeg", "-y", "-stream_loop", "-1",
            "-i", str(silent), "-t", f"{final_dur:.2f}",
            "-c", "copy", str(looped),
        ])
        silent = looped

    # Write ASS subtitles with karaoke highlighting
    ass_path = _write_ass(word_events, temp / "captions.ass")

    # Pass 1: burn subtitles onto video
    graded = temp / "graded.mp4"
    ass_posix = ass_path.resolve().as_posix()
    ass_escaped = (
        ass_posix.replace("\\", "\\\\").replace(":", "\\:")
        .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]")
    )
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", str(silent),
        "-vf", f"subtitles={ass_escaped}",
        "-t", f"{final_dur:.2f}",
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
        "-an", str(graded),
    ])

    # Pass 2: mix voice + background music with sidechain ducking
    voice_pad = f"apad=whole_dur={final_dur:.2f}"
    cmd = ["ffmpeg", "-y", "-i", str(graded), "-i", str(voiceover)]

    if music and music.exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(music)])
        cmd.extend([
            "-filter_complex",
            (
                f"[1:a]acompressor=threshold=-18dB:ratio=2.5:attack=5:release=120,{voice_pad}[va];"
                "[va]asplit=2[va1][va2];"
                "[2:a]highpass=f=80,lowpass=f=14000,volume=0.14[ma];"
                "[ma][va1]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=250[ducked];"
                "[va2][ducked]amix=inputs=2:duration=first:normalize=0[a]"
            ),
            "-map", "0:v", "-map", "[a]",
        ])
    else:
        cmd.extend([
            "-filter_complex", f"[1:a]{voice_pad}[a]",
            "-map", "0:v", "-map", "[a]",
        ])

    cmd.extend([
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{final_dur:.2f}", "-movflags", "+faststart",
        str(OUTPUT_PATH),
    ])
    _run_ffmpeg(cmd)
    print(f"[VIDEO] voice={voice_dur:.1f}s clips={clip_dur:.1f}s final={final_dur:.1f}s → {OUTPUT_PATH}")
    return OUTPUT_PATH


# ── YouTube Upload ───────────────────────────────────────────────────

def upload_video(meta: dict) -> str:
    """Upload long-form video to YouTube. Returns video_id."""
    creds = [os.getenv("YOUTUBE_CLIENT_ID"), os.getenv("YOUTUBE_CLIENT_SECRET"),
             os.getenv("YOUTUBE_REFRESH_TOKEN")]
    if not all(creds):
        print("[SKIP] Upload: missing credentials")
        return ""
    if not OUTPUT_PATH.is_file():
        print(f"[ERROR] Video not found: {OUTPUT_PATH}")
        return ""

    privacy = os.getenv("YOUTUBE_PRIVACY", "public")
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"

    # Get access token
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YOUTUBE_CLIENT_ID"],
        "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    }, timeout=30)
    resp.raise_for_status()
    access_token = resp.json()["access_token"]

    body = {
        "snippet": {
            "title": meta.get("title", "Salesforce Deep Dive")[:100],
            "description": meta.get("description", ""),
            "tags": meta.get("tags", _CORE_TAGS),
            "categoryId": "28",  # Science & Technology
            "defaultLanguage": "en",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
            "embeddable": True,
        },
    }

    video_data = OUTPUT_PATH.read_bytes()
    print(f"[UPLOAD] {len(video_data) / 1024 / 1024:.1f} MB...")

    init_resp = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(len(video_data)),
            "X-Upload-Content-Type": "video/mp4",
        },
        json=body, timeout=30,
    )
    init_resp.raise_for_status()
    upload_url = init_resp.headers["Location"]

    for attempt in range(1, 4):
        try:
            up_resp = requests.put(upload_url, headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "video/mp4",
                "Content-Length": str(len(video_data)),
            }, data=video_data, timeout=600)
            up_resp.raise_for_status()
            video_id = up_resp.json().get("id", "")
            print(f"[UPLOAD] Done! https://youtube.com/watch?v={video_id}")
            try:
                from analytics import log_upload
                log_upload(video_id, meta.get("title", ""), meta.get("topic", ""),
                           meta.get("tags", []), fmt="long")
            except Exception as exc:
                print(f"[WARN] Analytics: {exc}")
            return video_id
        except Exception as exc:
            print(f"[WARN] Upload attempt {attempt}: {exc}")
            if attempt < 3:
                time.sleep(attempt * 15)
    return ""


# ── Main Pipeline ────────────────────────────────────────────────────

def main():
    _clean_build_dir()

    # 1. Fetch fresh content
    print("[1/6] Fetching fresh Salesforce content...")
    content_item = _get_fresh_content()

    if content_item:
        article_title = content_item.title
        article_text = content_item.full_text or content_item.summary
        content_source = content_item.source
        print(f"  Source: {content_source}")
        print(f"  Title: {article_title}")
        print(f"  Text: {len(article_text.split())} words")

        if len(article_text.split()) < 100:
            print("[WARN] Article too short, trying another...")
            content_item = _get_fresh_content()
            if content_item:
                article_title = content_item.title
                article_text = content_item.full_text or content_item.summary
            else:
                article_title, article_text = _fallback_long_script()
    else:
        print("[WARN] No live content, using fallback facts")
        article_title, article_text = _fallback_long_script()

    # 2. Extract facts (Step 1 LLM)
    print("[2/6] Extracting key facts...")
    facts = step1_extract_facts(article_title, article_text)
    if not facts:
        print("[ERROR] Failed to extract facts")
        sys.exit(1)

    # 3. Generate script (Step 2 LLM)
    print("[3/6] Generating script...")
    script_data = None
    for attempt in range(2):
        script_data = step2_generate_script(facts, article_title)
        if script_data:
            break
        print(f"[RETRY] Script generation attempt {attempt + 2}...")

    if not script_data:
        print("[ERROR] Failed to generate script")
        sys.exit(1)

    script_text = script_data["script"]
    meta = {
        "title": script_data.get("title", article_title)[:100],
        "description": script_data.get("description", "") + _DESCRIPTION_FOOTER,
        "tags": list(dict.fromkeys(script_data.get("tags", []) + _CORE_TAGS))[:20],
        "topic": article_title,
    }

    # Save metadata for upload
    METADATA_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Title: {meta['title']}")
    print(f"  Script: {len(script_text.split())} words")

    # 4. TTS voiceover
    print("[4/6] Generating voiceover (edge-tts)...")
    audio_path, word_events = generate_tts(script_text)
    voice_dur = _probe_duration(audio_path)
    print(f"  Duration: {voice_dur:.1f}s ({voice_dur / 60:.1f} min)")

    # 5. Download clips + music
    print("[5/6] Downloading video clips...")
    pexels_queries = script_data.get("pexels_queries", [])
    clips = download_clips(extra_queries=pexels_queries, target=40)
    if not clips:
        print("[ERROR] No clips downloaded")
        sys.exit(1)

    music = download_music()

    # 6. Assemble video
    print("[6/6] Assembling video with ffmpeg...")
    assemble_video(clips, audio_path, word_events, music)

    # Upload
    print("[UPLOAD] Uploading to YouTube...")
    video_id = upload_video(meta)

    # Cleanup temp
    temp = BUILD_DIR / "temp"
    if temp.is_dir():
        shutil.rmtree(temp)

    if video_id:
        print(f"[DONE] Video uploaded: https://youtube.com/watch?v={video_id}")
    else:
        print("[DONE] Video generated but upload skipped/failed")


if __name__ == "__main__":
    main()
