# YouTube Salesforce Automation

Automated YouTube video generator for Salesforce content — **Shorts** (every 4 hours) and **Long-form** (daily deep-dives). Pulls fresh content from live sources: SalesforceBen, Salesforce Developer Blog, Reddit r/salesforce, Salesforce official blogs, and Trailhead.

## What it does

### Shorts (45–60 sec, 9:16 portrait)
1. **Fetches fresh content** from 5 live sources (RSS, Reddit API)
2. **Generates script** via Groq LLM based on real articles — always current and relevant
3. **Downloads stock video** clips from Pexels + Pixabay (tech visuals)
4. **Generates voice-over** using edge-tts with per-phrase sync
5. **Assembles 9:16 video** — Ken Burns zoom, bold subtitles, background music
6. **Uploads to YouTube** via OAuth2 Data API v3

### Long-form (8–12 min, 16:9 landscape)
1. **Fetches fresh article** from live sources (prefers rich content)
2. **Scrapes full article text** for deep analysis
3. **Two-step LLM pipeline**: extract key facts → generate 1200+ word script
4. **Generates TTS voiceover** with WordBoundary karaoke-style subtitles
5. **Assembles landscape video** with sidechain-ducked background music
6. **Uploads to YouTube** with full metadata

## Content Sources

| Source | Type | What it provides |
|--------|------|-----------------|
| **SalesforceBen.com** | RSS | Tips, tutorials, admin guides |
| **Salesforce Developer Blog** | RSS | Developer-focused updates |
| **Salesforce Blog / Admin Blog** | RSS | Official announcements, release notes |
| **Reddit r/salesforce** | JSON API | Community discussions, hot topics |
| **Trailhead Blog** | RSS | Learning content, new modules |

Content is deduplicated via `used_sources.json` — each article/post is used once, then the pool resets when exhausted.

## Content Quality

- LLM prompt demands specific Salesforce terms, menu paths, code snippets, and numbers
- Quality validation: min 8 parts, avg 8+ words, filler phrase detection, 40%+ concrete content
- Pronunciation fixes for 20+ Salesforce acronyms (SOQL, LWC, SFDX, etc.)
- Fallback scripts (shorts: 4 proven scripts; long: 2 pre-written fact sets) if all sources fail

## Setup

### 1. Create GitHub repo

Create a new repo and push this code.

### 2. Add Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Required | Description |
|--------|----------|-------------|
| `GROQ_API_KEY` | Yes | Free API key from [console.groq.com](https://console.groq.com) |
| `PEXELS_API_KEY` | Yes | Free API key from [pexels.com/api](https://www.pexels.com/api/) |
| `PIXABAY_API_KEY` | No | Free API key from [pixabay.com/api](https://pixabay.com/api/docs/) |
| `YOUTUBE_CLIENT_ID` | For upload | Google Cloud OAuth2 client ID |
| `YOUTUBE_CLIENT_SECRET` | For upload | Google Cloud OAuth2 client secret |
| `YOUTUBE_REFRESH_TOKEN` | For upload | Refresh token from OAuth2 flow |
| `YOUTUBE_PRIVACY` | No | `public` (default), `unlisted`, or `private` |

### 3. YouTube OAuth2 Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project, enable **YouTube Data API v3**
3. Create **OAuth 2.0 Client ID** (Desktop app type)
4. Add your Google account as a test user under **OAuth consent screen**
5. Use the [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/) to get a refresh token:
   - Settings gear → check "Use your own OAuth credentials" → paste Client ID & Secret
   - Step 1: authorize `https://www.googleapis.com/auth/youtube.upload` and `https://www.googleapis.com/auth/youtube.readonly`
   - Step 2: exchange for tokens → copy the **refresh_token**
6. Add all three values as GitHub Secrets

### 4. Run

- **Shorts**: every 4 hours via cron, or manual trigger
- **Long-form**: daily at 08:00 UTC, or manual trigger
- **Manual**: Actions tab → choose workflow → "Run workflow"

## Project Structure

```
generate_salesforce_short.py  — Shorts: live content → LLM → clips → TTS → video → upload
generate_salesforce_long.py   — Long-form: article → facts → script → TTS → ffmpeg → upload
sf_content_sources.py         — Content fetcher: RSS/Reddit/scraping + deduplication
upload_youtube.py             — YouTube OAuth2 resumable upload (shorts)
analytics.py                  — Performance tracking + weighted topic selection
used_sources.json             — Deduplication tracker for used articles/posts
performance_log.json          — Upload history + YouTube stats
requirements.txt              — Python dependencies
.github/workflows/
  generate_salesforce_short.yml — Shorts workflow (every 4h)
  generate_salesforce_long.yml  — Long-form workflow (daily)
```

## Architecture

```
Live Sources (5x RSS/API)
        │
        ▼
  sf_content_sources.py
   pick_fresh_content()
        │
        ├──► Shorts: generate_salesforce_short.py
        │     └── LLM script → edge-tts → MoviePy → YouTube
        │
        └──► Long:   generate_salesforce_long.py
              └── Scrape article → Extract facts (LLM) → Script (LLM)
                  → edge-tts + WordBoundary → FFmpeg + ASS subs → YouTube
```
