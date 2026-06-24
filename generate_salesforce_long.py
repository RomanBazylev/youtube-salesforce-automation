"""
Long-form Salesforce video generator (8–12 min, landscape 16:9).

Pipeline: fetch fresh content → scrape full article → extract facts (LLM) →
          generate structured script (LLM) → edge-tts with WordBoundary →
          Pexels clips per section → ffmpeg assembly (section titles, animated
          subtitles, lower-thirds, ducked music) → upload to YouTube

v2 — Major improvements:
  • Section-aware visuals: each script section gets its own relevant clips
  • Animated section title cards between segments
  • Better subtitle styling with background box + highlight
  • Optimized pipeline: fewer clips, single ffmpeg pass for assembly
  • Stronger LLM prompts for engaging, well-structured scripts
  • Intro hook card + outro CTA card
  • Timeout-safe: timing guards, reduced retries, faster clip prep
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
from typing import List, Optional, Dict

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

TARGET_W, TARGET_H = 1920, 1080
FPS = 30
FFMPEG_PRESET = "ultrafast"
FFMPEG_CRF = "22"

# Voice rotation for variety
TTS_VOICES = [
    "en-US-GuyNeural",
    "en-US-AndrewMultilingualNeural",
    "en-US-BrianMultilingualNeural",
]
TTS_RATE = "+0%"

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
    "SAML": "sam-el",
    "Agentforce": "agent-force",
    "Einstein": "ein-stine",
}

# Section-specific Pexels queries for better visuals
PEXELS_SECTION_QUERIES = {
    "intro": [
        "business professional working laptop modern office",
        "team collaboration whiteboard brainstorm",
        "futuristic digital interface hologram",
    ],
    "technical": [
        "code editor dark theme syntax highlighting",
        "developer typing laptop terminal dark",
        "software architecture diagram whiteboard",
        "database schema visualization",
        "API integration code screen dark",
    ],
    "admin": [
        "business dashboard analytics screen",
        "cloud computing data center blue lights",
        "IT administrator server monitoring",
        "workflow automation process diagram",
        "digital transformation office modern",
    ],
    "security": [
        "cybersecurity lock shield digital",
        "fingerprint biometric authentication",
        "network security firewall abstract",
        "encrypted data protection padlock",
    ],
    "conclusion": [
        "success achievement celebration professional",
        "business growth chart upward trend",
        "team meeting handshake agreement",
    ],
    "generic": [
        "modern office workspace dual monitors",
        "technology innovation abstract light",
        "data visualization 3D graph glow",
        "cloud infrastructure network connections",
        "professional workspace keyboard mouse clean",
        "digital data stream flowing blue",
        "software development team agile",
        "SaaS platform dashboard modern",
    ],
}

MUSIC_URLS = [
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Komiku/Its_time_for_adventure/Komiku_-_05_-_Friends.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Podington_Bear/Daydream/Podington_Bear_-_Daydream.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/ccCommunity/Chad_Crouch/Arps/Chad_Crouch_-_Shipping_Lanes.mp3",
    "https://files.freemusicarchive.org/storage-freemusicarchive-org/music/no_curator/Lobo_Loco/Folkish_things/Lobo_Loco_-_01_-_Acoustic_Dreams_ID_1199.mp3",
]

_CORE_TAGS = ["salesforce", "admin", "crm", "trailblazer", "automation", "tutorial",
              "salesforce tutorial", "salesforce admin", "salesforce developer"]

_DESCRIPTION_FOOTER = (
    "\n\n---\n"
    "⏰ Timestamps in the comments!\n"
    "#salesforce #salesforceadmin #salesforcedeveloper #crm #trailblazer #automation #tutorial\n\n"
    "🔔 Subscribe for weekly Salesforce deep-dives!\n"
    "👇 Drop your questions in the comments\n"
    "👍 Like if this helped you\n"
)

# ── Fallback scripts for when all live sources fail ────────────────────

_FALLBACK_FACTS = [
    (
        "Flow Builder Advanced Patterns in 2026",
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
    (
        "Agentforce and AI in Salesforce 2026",
        "1. Agentforce is Salesforce's AI agent platform — builds autonomous agents for service, sales, marketing.\n"
        "2. Einstein Copilot integrates into every Salesforce app — summarizes records, drafts emails, suggests next steps.\n"
        "3. Prompt Builder lets admins create custom AI prompts using merge fields from any Salesforce object.\n"
        "4. Data Cloud unifies customer data from all sources into a single profile for AI grounding.\n"
        "5. Trust Layer ensures AI responses are safe — toxicity detection, PII masking, audit trails.\n"
        "6. Einstein for Flow generates flow logic from natural language descriptions.\n"
        "7. Agentforce for Service resolves cases autonomously — escalates to humans only when needed.\n"
        "8. Custom AI actions in Agentforce connect to Apex, Flows, or external APIs.\n"
        "9. AI grounding with RAG pulls real-time knowledge from Knowledge articles and Data Cloud.\n"
        "10. Einstein Search uses semantic understanding — finds records even with misspellings or synonyms.",
    ),
]


# ── Helpers ────────────────────────────────────────────────────────────
def _clean_build_dir():
    if BUILD_DIR.is_dir():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)


def _run_ffmpeg(cmd: list, timeout: int = 300):
    print(f"[CMD] {' '.join(cmd[:8])}... ({len(cmd)} args)")
    subprocess.run(cmd, check=True, timeout=timeout)


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
        max_attempts = 5 if model == GROQ_MODEL else 3
        for attempt in range(1, max_attempts + 1):
            try:
                r = requests.post(GROQ_URL, headers=headers, json=body, timeout=90)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
            except requests.exceptions.HTTPError:
                if r.status_code == 429:
                    retry_after = r.headers.get("Retry-After")
                    if retry_after:
                        wait = min(int(float(retry_after)) + 2, 90)
                    else:
                        wait = min(10 * (2 ** (attempt - 1)), 90)
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
            item = scrape_full_article(item)
            return item
    except Exception as exc:
        print(f"[WARN] Live content fetch failed: {exc}")
    return None


# ── Two-Step LLM Pipeline ────────────────────────────────────────────

def step1_extract_facts(article_title: str, article_text: str) -> Optional[str]:
    """Step 1: Compress article into 8–12 key facts with actionable details."""
    words = article_text.split()
    if len(words) > 8000:
        article_text = " ".join(words[:8000])

    messages = [
        {"role": "system", "content": (
            "You are a senior Salesforce consultant and certified Technical Architect "
            "with 12+ years of hands-on experience. "
            "Your task is to extract 8-12 KEY ACTIONABLE facts from the article below.\n\n"
            "For EACH fact, include:\n"
            "- The WHAT: specific feature, setting, or concept name\n"
            "- The WHY: business impact or problem it solves\n"
            "- The HOW: navigation path (Setup > ...), code snippet, or step-by-step\n"
            "- A REAL-WORLD example or use case\n\n"
            "Write ONLY the facts — no introductions, no filler.\n"
            "Each fact = 2-4 sentences. Preserve specifics: feature names, paths, "
            "code snippets, numbers, version info, limitations, gotchas.\n"
            "Total output ~600-800 words."
        )},
        {"role": "user", "content": (
            f"Article title: {article_title}\n\n"
            f"Article text:\n{article_text}\n\n"
            "Extract 8-12 key actionable facts from this article."
        )},
    ]
    result = _groq_call(messages, temperature=0.3, max_tokens=3000)
    if result:
        print(f"[STEP1] Extracted facts: {len(result.split())} words")
    return result


def step2_generate_script(facts: str, article_title: str) -> Optional[dict]:
    """Step 2: Generate a structured, engaging YouTube script with sections."""
    messages = [
        {"role": "system", "content": (
            "You are a top Salesforce YouTuber running 'Salesforce Pro Tips' — a channel "
            "known for making complex Salesforce topics easy to understand with real examples. "
            "Your style combines authority (you're a certified Technical Architect) with "
            "approachability (you explain like helping a friend). You use storytelling, "
            "analogies, and real scenarios to make dry topics engaging.\n\n"
            "YOUR SIGNATURE STYLE:\n"
            "- Open with a BOLD hook that creates urgency or curiosity\n"
            "- Use analogies: 'Think of Permission Sets like keys on a keychain...'\n"
            "- Give SPECIFIC navigation paths: 'Go to Setup, type Permission, click Permission Set Groups'\n"
            "- Share 'pro tips' and 'gotchas' that show real experience\n"
            "- Each section flows naturally into the next with transitions\n"
            "- Vary sentence length: short punchy statements + longer explanations\n"
            "- Address the viewer directly: 'Here's what you need to do...'\n\n"
            "RULES:\n"
            "- Average sentence: 10-18 words (for clear TTS narration)\n"
            "- Include specific Salesforce terms, menu paths, code references\n"
            "- DO NOT copy source text — rewrite everything in YOUR voice\n"
            "- Each section needs a clear mini-conclusion before transitioning\n"
            "- Respond ONLY with valid JSON\n"
        )},
        {"role": "user", "content": f"""Write a YouTube video script (8-12 minutes spoken) about Salesforce.

TOPIC: {article_title}

KEY FACTS TO COVER:
{facts}

STRUCTURE — write ALL sections FULLY (aim for 1400-1800 total words):

1. HOOK (80-100 words):
   Start with ONE of these patterns:
   - Surprising stat: "Did you know that 73 percent of Salesforce orgs..."
   - Bold claim: "This one feature will save your team 10 hours a week."
   - Relatable pain: "If you've ever spent 3 hours debugging a flow..."
   - Question: "What if I told you there's a better way to..."
   Promise specific value the viewer will get.

2. CONTEXT (80-100 words):
   Brief background — why this topic matters RIGHT NOW.
   Mention recent Salesforce release, industry trend, or common pain point.
   "By the end of this video, you'll know exactly how to..."

3. MAIN CONTENT — 5-6 SECTIONS (each 180-220 words):
   Each section:
   - Transition phrase (varies! not the same each time)
   - Clear explanation of the concept/feature
   - WHY it matters (business impact)
   - HOW to do it (specific steps, paths, or code)
   - Pro tip OR common mistake to avoid
   - Mini-conclusion that bridges to next section

   Good transition examples (use different ones):
   - "Now here's where it gets really powerful..."
   - "But wait — there's a catch you need to know about."
   - "Let me show you the part most admins miss."
   - "This next one is a game-changer."
   - "Here's the thing nobody tells you..."
   - "Now let's take this to the next level."

4. SUMMARY (80-100 words):
   "Let's recap the key takeaways..."
   List 3-4 specific, actionable items the viewer should do TODAY.

5. CTA (40-50 words):
   Engage the audience: ask a specific question related to the topic.
   Mention subscribe, like, bell icon.

WORD MATH: Hook(90) + Context(90) + 5.5 sections x 200(1100) + Summary(90) + CTA(45) = ~1415 words minimum.
You MUST write at least 1300 words. This is a LONG-FORM video.

For EACH section, also suggest a "section_category" from: intro, technical, admin, security, conclusion, generic.
This helps pick relevant background footage.

JSON FORMAT:
{{
  "title": "Compelling title, max 85 chars, with 1-2 emoji, include 'Salesforce'",
  "description": "YouTube description: 8-10 lines. First line = hook sentence. Include timestamps placeholder. Mention what viewer will learn. Add 3 relevant links.",
  "tags": ["salesforce", ... 15-20 highly relevant tags],
  "sections": [
    {{
      "title": "Section title for title card (3-6 words)",
      "category": "intro|technical|admin|security|conclusion|generic",
      "script": "Full narration text for this section (as one paragraph).",
      "pexels_query": "specific search query for background footage for this section"
    }}
  ],
  "script": "FULL combined script as one string (all sections joined with newlines). Must be 1300+ words."
}}

IMPORTANT: The "sections" array gives structure. The "script" field is the FULL narration (all sections combined).
Both must be present. The script field is what gets narrated."""},
    ]
    content = _groq_call(messages, temperature=0.75, max_tokens=16384, json_mode=True)
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

        # If script is empty but sections exist, reconstruct
        if not script and data.get("sections"):
            script = "\n\n".join(s.get("script", "") for s in data["sections"])
            data["script"] = script

        word_count = len(script.split())
        print(f"[STEP2] Script generated: {word_count} words, {len(data.get('sections', []))} sections")
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


def download_clips_for_sections(sections: list, target_per_section: int = 4) -> Dict[int, List[Path]]:
    """Download clips organized by section for section-aware visuals."""
    api_key = os.getenv("PEXELS_API_KEY")
    if not api_key:
        print("[WARN] No PEXELS_API_KEY")
        return {}

    headers = {"Authorization": api_key}
    section_clips: Dict[int, List[Path]] = {}
    seen_ids = set()
    clip_idx = 0

    for sec_idx, section in enumerate(sections):
        section_clips[sec_idx] = []
        category = section.get("category", "generic")
        pexels_query = section.get("pexels_query", "")

        # Build query list: section-specific + category defaults
        queries = []
        if pexels_query:
            queries.append(pexels_query)
        queries.extend(PEXELS_SECTION_QUERIES.get(category, []))
        queries.extend(random.sample(PEXELS_SECTION_QUERIES["generic"],
                                     min(2, len(PEXELS_SECTION_QUERIES["generic"]))))

        for query in queries:
            if len(section_clips[sec_idx]) >= target_per_section:
                break
            try:
                resp = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers=headers,
                    params={"query": query, "per_page": 3, "orientation": "landscape",
                            "size": "medium"},
                    timeout=30,
                )
                resp.raise_for_status()
            except Exception as exc:
                print(f"[WARN] Pexels '{query}': {exc}")
                continue

            for video in resp.json().get("videos", []):
                if len(section_clips[sec_idx]) >= target_per_section:
                    break
                vid_id = video.get("id")
                if vid_id in seen_ids:
                    continue
                seen_ids.add(vid_id)
                # Prefer HD but accept 720p
                files = video.get("video_files", [])
                hd = [f for f in files if 720 <= (f.get("height") or 0) <= 1080]
                if not hd:
                    hd = [f for f in files if (f.get("height") or 0) >= 480]
                if not hd:
                    continue
                best = min(hd, key=lambda f: abs((f.get("height") or 0) - 1080))
                clip_idx += 1
                clip_path = CLIPS_DIR / f"clip_{clip_idx:03d}.mp4"
                try:
                    _download_file(best["link"], clip_path)
                    section_clips[sec_idx].append(clip_path)
                except Exception:
                    pass

    total = sum(len(v) for v in section_clips.values())
    print(f"[CLIPS] Downloaded {total} clips across {len(sections)} sections")
    return section_clips


def download_music() -> Optional[Path]:
    for url in random.sample(MUSIC_URLS, len(MUSIC_URLS)):
        try:
            _download_file(url, MUSIC_PATH)
            return MUSIC_PATH
        except Exception:
            continue
    return None


# ── FFmpeg Assembly ──────────────────────────────────────────────────

def _prepare_clip(src: Path, dst: Path, duration: int = 6):
    """Scale, crop and normalize a clip to target resolution."""
    vf = (
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H},fps={FPS},"
        "eq=brightness=0.02:contrast=1.05:saturation=1.1"
    )
    _run_ffmpeg([
        "ffmpeg", "-y", "-i", str(src), "-t", str(duration),
        "-vf", vf, "-an", "-c:v", "libx264",
        "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF, str(dst),
    ], timeout=60)


def _generate_title_card(text: str, dst: Path, duration: float = 2.5):
    """Generate a title card with text overlay using ffmpeg."""
    safe_text = text.replace("'", "").replace(":", " -").replace("\\", "")
    safe_text = safe_text[:50]

    _run_ffmpeg([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"color=c=0x1a1a2e:s={TARGET_W}x{TARGET_H}:d={duration}:r={FPS}",
        "-vf", (
            f"drawtext=text='{safe_text}':"
            f"fontsize=64:fontcolor=white:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:"
            f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"borderw=3:bordercolor=0x0066ff,"
            f"drawtext=text='SALESFORCE PRO TIPS':"
            f"fontsize=30:fontcolor=0x00aaff:"
            f"x=(w-text_w)/2:y=h-120:"
            f"fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        ),
        "-t", str(duration), "-c:v", "libx264",
        "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
        "-pix_fmt", "yuv420p", str(dst),
    ], timeout=30)


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


def _group_words(word_events: list, max_per_line: int = 7) -> list:
    """Group word events into subtitle lines with better phrasing."""
    if not word_events:
        return []
    lines = []
    buf_words, buf_start, buf_end, buf_kara = [], 0.0, 0.0, []
    for ev in word_events:
        start, dur = ev["offset"], ev["duration"]
        end = start + dur
        if buf_words and (len(buf_words) >= max_per_line or (start - buf_end) > 0.5):
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
    """Write ASS subtitle file with modern styling — boxed background + karaoke."""
    font_size = 52
    margin_v = 60
    # Modern color scheme: blue highlight for spoken words, white for upcoming
    primary = "&H0000CCFF"     # Bright amber/gold (highlighted/spoken)
    secondary = "&H00FFFFFF"   # White (default)
    outline = "&H00000000"     # Black outline
    shadow = "&HAA000000"      # Semi-transparent black background box

    header = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\n"
        f"PlayResX: {TARGET_W}\nPlayResY: {TARGET_H}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Kara,DejaVu Sans,{font_size},{primary},{secondary},{outline},{shadow},"
        f"1,0,0,0,100,100,1.5,0,3,2,4,2,50,50,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines = _group_words(word_events)
    events = []
    for line in lines:
        start = line["start"]
        end = line["end"] + 0.2
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
    print(f"[SUBS] {len(events)} lines, {len(word_events)} words -> {ass_path}")
    return ass_path


def assemble_video(
    section_clips: Dict[int, List[Path]],
    sections: list,
    voiceover: Path,
    word_events: list,
    music: Optional[Path],
) -> Path:
    """Assemble final video with section title cards and section-aware clips."""
    temp = BUILD_DIR / "temp"
    temp.mkdir(exist_ok=True)

    voice_dur = _probe_duration(voiceover)
    final_dur = voice_dur + 2.0

    # Prepare all clips (6 sec each)
    all_prepared = []
    prep_idx = 0
    for sec_idx in sorted(section_clips.keys()):
        for clip in section_clips[sec_idx]:
            dst = temp / f"prep_{prep_idx:03d}.mp4"
            _prepare_clip(clip, dst, duration=6)
            all_prepared.append(dst)
            prep_idx += 1

    if not all_prepared:
        print("[ERROR] No clips prepared")
        sys.exit(1)

    # Generate section title cards (skip first and last section)
    title_cards = []
    if sections and len(sections) > 2:
        for i, sec in enumerate(sections[1:-1], 1):
            title = sec.get("title", f"Section {i}")
            card_path = temp / f"title_{i:02d}.mp4"
            try:
                _generate_title_card(title, card_path, duration=2.5)
                title_cards.append(card_path)
            except Exception as exc:
                print(f"[WARN] Title card {i} failed: {exc}")

    # Build concat list: interleave title cards with clips
    concat_parts = []
    clips_per_section = max(1, len(all_prepared) // max(1, len(sections)))
    clip_pointer = 0

    for sec_idx in range(len(sections)):
        # Add title card before each main section (not for intro)
        if sec_idx > 0 and sec_idx - 1 < len(title_cards):
            concat_parts.append(title_cards[sec_idx - 1])

        # Add clips for this section
        n_clips = clips_per_section
        if sec_idx == 0:
            n_clips = max(2, clips_per_section)
        for _ in range(n_clips):
            if clip_pointer < len(all_prepared):
                concat_parts.append(all_prepared[clip_pointer])
                clip_pointer += 1
            else:
                clip_pointer = 0
                concat_parts.append(all_prepared[clip_pointer])
                clip_pointer += 1

    # Add remaining clips
    while clip_pointer < len(all_prepared):
        concat_parts.append(all_prepared[clip_pointer])
        clip_pointer += 1

    if not concat_parts:
        concat_parts = all_prepared

    # Concatenate all parts
    concat_file = temp / "concat.txt"
    concat_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in concat_parts),
        encoding="utf-8",
    )
    silent = temp / "silent.mp4"
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0",
                 "-i", str(concat_file), "-c", "copy", str(silent)], timeout=120)

    clip_dur = _probe_duration(silent)

    # Loop video if shorter than voiceover
    if clip_dur < final_dur:
        looped = temp / "looped.mp4"
        _run_ffmpeg([
            "ffmpeg", "-y", "-stream_loop", "-1",
            "-i", str(silent), "-t", f"{final_dur:.2f}",
            "-c", "copy", str(looped),
        ], timeout=60)
        silent = looped

    # Write ASS subtitles with karaoke highlighting
    ass_path = _write_ass(word_events, temp / "captions.ass")

    # Single-pass: burn subtitles + mix audio
    ass_posix = ass_path.resolve().as_posix()
    ass_escaped = (
        ass_posix.replace("\\", "\\\\").replace(":", "\\:")
        .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]")
    )

    cmd = ["ffmpeg", "-y", "-i", str(silent), "-i", str(voiceover)]

    if music and music.exists():
        cmd.extend(["-stream_loop", "-1", "-i", str(music)])
        cmd.extend([
            "-filter_complex",
            (
                f"[0:v]subtitles={ass_escaped}[v];"
                f"[1:a]acompressor=threshold=-18dB:ratio=2.5:attack=5:release=120,"
                f"apad=whole_dur={final_dur:.2f}[va];"
                "[va]asplit=2[va1][va2];"
                "[2:a]highpass=f=80,lowpass=f=14000,volume=0.12[ma];"
                "[ma][va1]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=250[ducked];"
                "[va2][ducked]amix=inputs=2:duration=first:normalize=0[a]"
            ),
            "-map", "[v]", "-map", "[a]",
        ])
    else:
        cmd.extend([
            "-filter_complex",
            (
                f"[0:v]subtitles={ass_escaped}[v];"
                f"[1:a]apad=whole_dur={final_dur:.2f}[a]"
            ),
            "-map", "[v]", "-map", "[a]",
        ])

    cmd.extend([
        "-c:v", "libx264", "-preset", FFMPEG_PRESET, "-crf", FFMPEG_CRF,
        "-c:a", "aac", "-b:a", "192k",
        "-t", f"{final_dur:.2f}", "-movflags", "+faststart",
        str(OUTPUT_PATH),
    ])
    _run_ffmpeg(cmd, timeout=900)
    print(f"[VIDEO] voice={voice_dur:.1f}s clips={clip_dur:.1f}s final={final_dur:.1f}s -> {OUTPUT_PATH}")
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
    start_time = time.time()
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
    print("[3/6] Generating structured script...")
    script_data = None
    for attempt in range(3):
        script_data = step2_generate_script(facts, article_title)
        if script_data:
            break
        print(f"[RETRY] Script generation attempt {attempt + 2}...")

    if not script_data:
        print("[ERROR] Failed to generate script")
        sys.exit(1)

    script_text = script_data["script"]
    sections = script_data.get("sections", [])
    meta = {
        "title": script_data.get("title", article_title)[:100],
        "description": script_data.get("description", "") + _DESCRIPTION_FOOTER,
        "tags": list(dict.fromkeys(script_data.get("tags", []) + _CORE_TAGS))[:20],
        "topic": article_title,
    }

    # Save metadata
    METADATA_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Title: {meta['title']}")
    print(f"  Script: {len(script_text.split())} words")
    print(f"  Sections: {len(sections)}")

    # 4. TTS voiceover
    print("[4/6] Generating voiceover (edge-tts)...")
    audio_path, word_events = generate_tts(script_text)
    voice_dur = _probe_duration(audio_path)
    print(f"  Duration: {voice_dur:.1f}s ({voice_dur / 60:.1f} min)")

    # Check elapsed time — abort if too slow
    elapsed = time.time() - start_time
    print(f"  Elapsed so far: {elapsed:.0f}s")
    if elapsed > 3600:
        print("[ERROR] Pipeline too slow (>60min at TTS stage), aborting")
        sys.exit(1)

    # 5. Download clips per section + music
    print("[5/6] Downloading video clips (section-aware)...")
    if sections:
        section_clips = download_clips_for_sections(sections, target_per_section=4)
    else:
        # Fallback: use generic queries
        section_clips = download_clips_for_sections(
            [{"category": "generic", "pexels_query": q}
             for q in random.sample(PEXELS_SECTION_QUERIES["generic"], 4)],
            target_per_section=5,
        )

    total_clips = sum(len(v) for v in section_clips.values())
    if total_clips == 0:
        print("[ERROR] No clips downloaded")
        sys.exit(1)

    music = download_music()

    # Check elapsed time
    elapsed = time.time() - start_time
    print(f"  Elapsed after downloads: {elapsed:.0f}s")
    if elapsed > 4200:  # 70 min
        print("[ERROR] Pipeline too slow (>70min at download stage), aborting")
        sys.exit(1)

    # 6. Assemble video
    print("[6/6] Assembling video with ffmpeg...")
    assemble_video(section_clips, sections, audio_path, word_events, music)

    # Upload
    print("[UPLOAD] Uploading to YouTube...")
    video_id = upload_video(meta)

    # Cleanup temp
    temp = BUILD_DIR / "temp"
    if temp.is_dir():
        shutil.rmtree(temp)

    total_time = time.time() - start_time
    print(f"[TIMING] Total pipeline: {total_time:.0f}s ({total_time / 60:.1f} min)")

    if video_id:
        print(f"[DONE] Video uploaded: https://youtube.com/watch?v={video_id}")
    else:
        print("[DONE] Video generated but upload skipped/failed")


if __name__ == "__main__":
    main()
