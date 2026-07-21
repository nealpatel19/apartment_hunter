# SF Apartment Scanner 🏙️

> A production-ready, fully serverless Python pipeline that automatically scans San Francisco Craigslist listings, AI-audits them with Gemini, scores them with deterministic weighted math, and emails you a beautiful digest — three times a day, for free.

---

## Architecture Overview

```
Craigslist RSS
     │
     ▼
[ingestion.py]  — Fetch & normalize 2BR listings, apply price floor/cap
     │
     ▼
[state.py]      — Deduplicate via GitHub Gist (seen IDs + timestamps, 45-day TTL)
     │
     ▼
[auditor.py]    — Gemini 2.5 Flash: scam/room-share detection + structured extraction
     │
     ▼
[scorer.py]     — Deterministic weighted scoring (Layout 35%, Price 30%, Location 20%, Amenities 15%)
     │
     ▼
[notifier.py]   — Dark-mode HTML email digest with one-click landlord outreach via Resend
```

**Runtime cost: ~$0/month** — GitHub Actions (free tier), Gemini API (generous free quota), Resend (3,000 emails/month free), GitHub Gist (free).

---

## Prerequisites

- Python 3.10+
- A GitHub account (for Actions + Gist)
- API accounts for Google AI and Resend (both have free tiers)

---

## Step 1: Get Your API Keys

### 1a. Google Gemini API Key (`GEMINI_API_KEY`)

1. Go to [Google AI Studio](https://aistudio.google.com/app/apikey)
2. Click **"Create API key"**
3. Copy the generated key — this is your `GEMINI_API_KEY`

### 1b. Resend API Key (`RESEND_API_KEY`)

1. Sign up at [resend.com](https://resend.com)
2. In the dashboard, go to **API Keys → Create API Key**
3. Name it `sf-apartment-scanner` and copy it — this is your `RESEND_API_KEY`
4. **Email verification**: On the free plan, you can only send to your own verified email. Go to **Domains** or use the sandbox sender `onboarding@resend.dev` for testing

### 1c. GitHub Gist Token (`GIST_TOKEN`) and Gist ID (`GIST_ID`)

**Create the Gist (state store):**

1. Go to [gist.github.com](https://gist.github.com)
2. Create a new **secret** Gist with:
   - **Filename**: `sf_seen_listings.json`
   - **Content**: `{"seen": {}}`
3. After saving, copy the Gist ID from the URL: `https://gist.github.com/YOUR_USERNAME/YOUR_GIST_ID`
   - This is your `GIST_ID`

**Create a Personal Access Token:**

1. Go to [GitHub Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens](https://github.com/settings/personal-access-tokens/new)
2. Set:
   - **Token name**: `sf-apartment-scanner`
   - **Expiration**: 1 year (or no expiration)
   - **Permissions**: Under **Account permissions**, enable `Gist` → **Read and Write**
3. Click **Generate token** and copy it — this is your `GIST_TOKEN`

---

## Step 2: Fork / Clone This Repository

```bash
git clone https://github.com/YOUR_USERNAME/sf-apartment-scanner.git
cd sf-apartment-scanner
```

Or fork it directly on GitHub and clone your fork.

---

## Step 3: Configure GitHub Secrets

In your repository on GitHub:

1. Go to **Settings → Secrets and variables → Actions**
2. Click **"New repository secret"** for each of the following:

| Secret Name          | Where to Get It                        |
|----------------------|----------------------------------------|
| `GEMINI_API_KEY`     | Google AI Studio (Step 1a)             |
| `RESEND_API_KEY`     | Resend dashboard (Step 1b)             |
| `GIST_TOKEN`         | GitHub fine-grained PAT (Step 1c)      |
| `GIST_ID`            | From your Gist URL (Step 1c)           |
| `NOTIFICATION_EMAIL` | Your personal email address            |

---

## Step 4: Test Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export GEMINI_API_KEY="your_key_here"
export RESEND_API_KEY="your_key_here"
export GIST_TOKEN="your_token_here"
export GIST_ID="your_gist_id_here"
export NOTIFICATION_EMAIL="you@example.com"

# Run the full pipeline
python main.py
```

---

## Step 5: Deploy — Push to GitHub

```bash
git add .
git commit -m "Initial deploy"
git push origin main
```

The GitHub Actions workflow (`.github/workflows/scanner.yml`) will automatically run at:

| Time       | Cron (UTC)    | Pacific Time         |
|------------|---------------|----------------------|
| 8:00 AM PT | `0 16 * * *`  | Morning sweep        |
| 5:00 PM PT | `0 1  * * *`  | After-work sweep     |
| 9:00 PM PT | `0 5  * * *`  | Evening sweep        |

You can also trigger a manual run from the **Actions** tab → **Run workflow**.

---

## Scoring Formula

```
Total Score = 0.35 × Layout + 0.30 × Price + 0.20 × Location + 0.15 × Amenities
```

| Component | Weight | Logic |
|-----------|--------|-------|
| **Layout**    | 35% | 100 pts for true 2+ BR, 0 otherwise |
| **Price**     | 30% | 100 pts ≤ $3,800/mo; 0 pts ≥ $4,400/mo; linear between |
| **Location**  | 20% | 94110/94117/94102 = 100 pts; 94107 = 80 pts; other SF = 50 pts |
| **Amenities** | 15% | In-unit laundry (+35), Outdoor space (+35), Dishwasher (+30); max 100 |

Listings below **60 points** are filtered — no email is sent.

---

## Target Neighborhoods

| Zip Code | Neighborhood              | Location Tier |
|----------|---------------------------|---------------|
| 94110    | Mission / Bernal Heights  | Tier 1 (100)  |
| 94117    | Haight / Cole Valley      | Tier 1 (100)  |
| 94102    | Hayes Valley / Civic Center | Tier 1 (100) |
| 94107    | SOMA / Potrero Hill       | Tier 2 (80)   |

---

## State Management

- Seen listing IDs are stored in a GitHub Gist as `{"seen": {"listing_id": "ISO-8601 timestamp"}}`
- Entries are automatically pruned after **45 days** to prevent unbounded growth
- The Gist is read at the start of each run and written at the end (even if no listings qualified)

---

## Project Structure

```
sf-apartment-scanner/
├── main.py           # Pipeline orchestrator
├── ingestion.py      # Craigslist RSS fetching & normalization
├── state.py          # GitHub Gist deduplication & state management
├── auditor.py        # Gemini AI listing analysis (Pydantic structured output)
├── scorer.py         # Deterministic weighted scoring engine
├── notifier.py       # HTML email digest via Resend
├── requirements.txt  # Pinned dependencies
├── .github/
│   └── workflows/
│       └── scanner.yml  # GitHub Actions cron workflow
└── README.md
```

---

## Troubleshooting

**No email received?**
- Check GitHub Actions logs under the **Actions** tab
- Verify Resend only allows sending to your verified email on the free plan
- Ensure all secrets are set correctly

**"Gist file not found" warning?**
- Make sure your Gist file is named exactly `sf_seen_listings.json`
- Verify the `GIST_ID` is the full ID from the Gist URL (not the username)

**Gemini rate limit errors?**
- The pipeline has built-in delays between API calls
- Consider reducing scan frequency if you're on a low-quota tier

**No listings being found?**
- Craigslist occasionally changes their RSS URL structure
- Run locally with `DEBUG` logging: `logging.basicConfig(level=logging.DEBUG)`
