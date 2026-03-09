# YouTube Salesforce Shorts Automation

Automated YouTube Shorts generator for Salesforce tips, tricks, and best practices. Runs on GitHub Actions every 4 hours.

## What it does

1. **Generates script** via Groq LLM (llama-3.3-70b-versatile) — real Salesforce tips with specific features, navigation paths, formulas
2. **Downloads stock video** clips from Pexels + Pixabay (tech/business visuals)
3. **Generates voice-over** using edge-tts (en-US-GuyNeural) with per-phrase sync
4. **Assembles 9:16 video** — Ken Burns zoom, bold subtitles, background music
5. **Uploads to YouTube** via OAuth2 Data API v3
6. **Quality gate** — validates script for substance (rejects filler content)

## Content Quality

- LLM prompt demands specific Salesforce terms, menu paths, code snippets, and numbers
- Quality validation checks: min 8 parts, avg 8+ words, filler phrase detection, 40%+ concrete content
- Pronunciation fixes for 20+ Salesforce acronyms (SOQL, LWC, SFDX, etc.)
- Fallback script with proven Flow Builder tips if LLM output is weak

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
   - Step 1: authorize `https://www.googleapis.com/auth/youtube.upload`
   - Step 2: exchange for tokens → copy the **refresh_token**
6. Add all three values as GitHub Secrets

### 4. Run

- **Automatic**: runs every 4 hours via cron
- **Manual**: Actions tab → "Generate Salesforce Short" → "Run workflow"

## Project Structure

```
generate_salesforce_short.py  — Main script: LLM → clips → TTS → video
upload_youtube.py             — YouTube OAuth2 resumable upload
requirements.txt              — Python dependencies
.github/workflows/            — GitHub Actions workflow
```

## Topics Covered

The generator randomly combines:
- **27 Salesforce topics**: Flow Builder, Apex, LWC, Reports, Permission Sets, SOQL, etc.
- **12 content angles**: hidden features, common mistakes, quick automations, myth vs reality, etc.
- **6 audience levels**: admin, developer, consultant, architect, beginner, business analyst

This gives **1,944 unique combinations** before any repeats.
