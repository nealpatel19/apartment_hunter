"""
notifier.py — Email Dispatcher via Gmail SMTP

Generates a premium dark-mode HTML email digest of qualified listings
and dispatches it directly via Gmail SMTP (smtplib). Supports sending
to multiple recipient emails without domain verification.
"""

from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from scorer import ScoredListing

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SMTP_HOST: str = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))

# Sender credentials
SMTP_EMAIL: str = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")

# Recipient email list (comma-separated: "email1@gmail.com, email2@gmail.com")
NOTIFICATION_EMAIL_RAW: str = os.environ.get("NOTIFICATION_EMAIL", "")
RECIPIENT_EMAILS: list[str] = [
    e.strip() for e in NOTIFICATION_EMAIL_RAW.split(",") if e.strip()
]

# ---------------------------------------------------------------------------
# Score badge color logic
# ---------------------------------------------------------------------------


def _badge_color(score: float) -> tuple[str, str]:
    """Return (bg_color, text_color) hex strings for a score badge."""
    if score >= 85:
        return "#10b981", "#ffffff"  # emerald — excellent
    if score >= 70:
        return "#3b82f6", "#ffffff"  # blue — good
    if score >= 60:
        return "#f59e0b", "#1a1a1a"  # amber — borderline
    return "#6b7280", "#ffffff"      # gray — fallback


# ---------------------------------------------------------------------------
# Neighborhood label lookup
# ---------------------------------------------------------------------------

NEIGHBORHOOD_LABELS: dict[str, str] = {
    "94110": "Mission / Bernal Heights",
    "94117": "Haight / Cole Valley",
    "94102": "Hayes Valley / Civic Center",
    "94107": "SOMA / Potrero Hill",
}


def _neighborhood_label(zip_code: str) -> str:
    return NEIGHBORHOOD_LABELS.get(zip_code, f"San Francisco ({zip_code})")


# ---------------------------------------------------------------------------
# HTML building blocks
# ---------------------------------------------------------------------------


def _render_score_breakdown(scored: ScoredListing) -> str:
    bars = [
        ("Layout",    scored.layout_score,    "#8b5cf6"),
        ("Price",     scored.price_score,     "#3b82f6"),
        ("Location",  scored.location_score,  "#10b981"),
        ("Amenities", scored.amenities_score, "#f59e0b"),
    ]
    rows = ""
    for label, val, color in bars:
        pct = int(val)
        rows += f"""
        <div style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;margin-bottom:4px;">
            <span style="font-size:12px;color:#9ca3af;">{label}</span>
            <span style="font-size:12px;color:#e5e7eb;font-weight:600;">{val:.0f}</span>
          </div>
          <div style="background:#374151;border-radius:4px;height:6px;">
            <div style="background:{color};width:{pct}%;height:6px;border-radius:4px;"></div>
          </div>
        </div>"""
    return rows


def _render_listing_card(scored: ScoredListing, rank: int) -> str:
    listing = scored.listing
    analysis = scored.analysis
    bg_color, text_color = _badge_color(scored.total_score)

    neighborhood = _neighborhood_label(listing.zip_code)

    # Laundry icon + label
    laundry_icon = {"in-unit": "🫧", "in-building": "🏢", "none": "❌"}.get(
        analysis.laundry_type, "❓"
    )
    laundry_label = {
        "in-unit": "In-Unit Laundry",
        "in-building": "Shared Building Laundry",
        "none": "No Laundry",
    }.get(analysis.laundry_type, "Unknown Laundry")

    # One-click mailto with pre-filled pitch
    subject = f"Inquiry About Your Listing: {listing.title}"
    mailto_body = analysis.custom_outreach_pitch.replace("\n", "%0A").replace('"', "%22")
    mailto_href = f"mailto:?subject={subject.replace(' ', '%20')}&body={mailto_body}"

    # Parking
    parking_str = (
        f"${analysis.parking_monthly_fee}/mo" if analysis.parking_monthly_fee > 0 else "Included / None"
    )

    score_bars = _render_score_breakdown(scored)

    return f"""
    <div style="background:#1f2937;border-radius:16px;padding:28px;margin-bottom:24px;border:1px solid #374151;">

      <!-- Header row -->
      <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:12px;">
        <div>
          <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px;">
            #{rank} &nbsp;·&nbsp; {neighborhood}
          </div>
          <h2 style="margin:0 0 6px;font-size:18px;color:#f9fafb;font-weight:700;line-height:1.3;">
            <a href="{listing.url}" style="color:#f9fafb;text-decoration:none;">{listing.title}</a>
          </h2>
          <div style="font-size:26px;color:#10b981;font-weight:800;">${listing.price:,}<span style="font-size:14px;font-weight:400;color:#9ca3af;">/mo</span></div>
        </div>
        <!-- Score badge -->
        <div style="background:{bg_color};color:{text_color};border-radius:50%;width:70px;height:70px;display:flex;align-items:center;justify-content:center;flex-direction:column;flex-shrink:0;">
          <div style="font-size:22px;font-weight:800;line-height:1;">{scored.total_score:.0f}</div>
          <div style="font-size:9px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">Score</div>
        </div>
      </div>

      <!-- Quick amenity chips -->
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px;">
        <span style="background:#111827;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;color:#d1d5db;">{laundry_icon} {laundry_label}</span>
        {"<span style='background:#111827;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;color:#d1d5db;'>🌿 Outdoor Space</span>" if analysis.has_outdoor_space else ""}
        {"<span style='background:#111827;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;color:#d1d5db;'>🍽️ Dishwasher</span>" if analysis.has_dishwasher else ""}
        <span style="background:#111827;border:1px solid #374151;border-radius:20px;padding:4px 12px;font-size:12px;color:#d1d5db;">🚗 Parking: {parking_str}</span>
      </div>

      <!-- Score breakdown bars -->
      <div style="background:#111827;border-radius:10px;padding:16px;margin-bottom:20px;">
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#6b7280;margin-bottom:12px;">Score Breakdown</div>
        {score_bars}
      </div>

      <!-- AI Pro / Con -->
      <div style="display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;">
        <div style="flex:1;min-width:200px;background:#064e3b;border-left:3px solid #10b981;border-radius:0 8px 8px 0;padding:12px 14px;">
          <div style="font-size:10px;color:#6ee7b7;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">✅ Pro</div>
          <div style="font-size:13px;color:#d1fae5;">{analysis.ai_pro_bullet}</div>
        </div>
        <div style="flex:1;min-width:200px;background:#431407;border-left:3px solid #ef4444;border-radius:0 8px 8px 0;padding:12px 14px;">
          <div style="font-size:10px;color:#fca5a5;text-transform:uppercase;letter-spacing:1px;margin-bottom:4px;">⚠️ Con</div>
          <div style="font-size:13px;color:#fee2e2;">{analysis.ai_con_bullet}</div>
        </div>
      </div>

      <!-- Outreach pitch box -->
      <div style="background:#1e3a5f;border:1px solid #1d4ed8;border-radius:10px;padding:16px;margin-bottom:20px;">
        <div style="font-size:10px;color:#93c5fd;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">📝 AI-Crafted Outreach Pitch</div>
        <div style="font-size:13px;color:#bfdbfe;line-height:1.6;font-style:italic;">"{analysis.custom_outreach_pitch}"</div>
      </div>

      <!-- CTA buttons -->
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <a href="{listing.url}"
           style="display:inline-block;background:#3b82f6;color:#ffffff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
          🔗 View Listing
        </a>
        <a href="{mailto_href}"
           style="display:inline-block;background:#10b981;color:#ffffff;padding:10px 20px;border-radius:8px;text-decoration:none;font-size:13px;font-weight:600;">
          ✉️ One-Click Email Landlord
        </a>
      </div>

    </div>
    """


# ---------------------------------------------------------------------------
# Full email template
# ---------------------------------------------------------------------------


def _build_html_email(scored_listings: list[ScoredListing]) -> str:
    now = datetime.now(tz=timezone.utc).strftime("%B %d, %Y at %I:%M %p UTC")
    count = len(scored_listings)

    cards_html = "\n".join(
        _render_listing_card(s, idx + 1) for idx, s in enumerate(scored_listings)
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>SF Apartment Scout — {count} New Match{'es' if count != 1 else ''}</title>
</head>
<body style="margin:0;padding:0;background:#0f172a;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">

  <div style="max-width:680px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="text-align:center;padding:40px 0 32px;">
      <div style="font-size:36px;margin-bottom:8px;">🏙️</div>
      <h1 style="margin:0 0 8px;font-size:28px;color:#f9fafb;font-weight:800;letter-spacing:-0.5px;">
        SF Apartment Scout
      </h1>
      <p style="margin:0;color:#6b7280;font-size:14px;">{now}</p>
      <div style="margin-top:16px;display:inline-block;background:#1f2937;border:1px solid #374151;border-radius:20px;padding:8px 20px;">
        <span style="color:#10b981;font-weight:700;font-size:16px;">{count}</span>
        <span style="color:#9ca3af;font-size:14px;"> new match{'es' if count != 1 else ''} above score threshold</span>
      </div>
    </div>

    <!-- Summary strip -->
    <div style="background:#1f2937;border-radius:12px;padding:16px 20px;margin-bottom:28px;display:flex;gap:20px;flex-wrap:wrap;">
      <div style="flex:1;min-width:120px;text-align:center;">
        <div style="font-size:22px;font-weight:800;color:#3b82f6;">{count}</div>
        <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;">Listings</div>
      </div>
      <div style="flex:1;min-width:120px;text-align:center;">
        <div style="font-size:22px;font-weight:800;color:#10b981;">${min(s.listing.price for s in scored_listings):,}</div>
        <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;">Lowest Rent</div>
      </div>
      <div style="flex:1;min-width:120px;text-align:center;">
        <div style="font-size:22px;font-weight:800;color:#f59e0b;">{scored_listings[0].total_score:.0f}</div>
        <div style="font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.5px;">Top Score</div>
      </div>
    </div>

    <!-- Listing cards -->
    {cards_html}

    <!-- Footer -->
    <div style="text-align:center;padding:32px 0 16px;border-top:1px solid #1f2937;margin-top:8px;">
      <p style="color:#4b5563;font-size:12px;margin:0 0 4px;">
        Powered by Gemini 2.5 Flash · SF Apartment Scanner
      </p>
      <p style="color:#374151;font-size:11px;margin:0;">
        Listings sourced from Craigslist SF · Scores are AI-assisted estimates
      </p>
    </div>

  </div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def send_digest(scored_listings: list[ScoredListing]) -> None:
    """
    Send the HTML email digest via Gmail SMTP (smtplib).

    If the list is empty, logs and returns without sending.
    """
    if not scored_listings:
        logger.info("No qualified listings to send — skipping email dispatch.")
        return

    if not RECIPIENT_EMAILS:
        logger.error("No recipient emails configured in NOTIFICATION_EMAIL.")
        return

    if not SMTP_EMAIL or not SMTP_PASSWORD:
        logger.error("SMTP_EMAIL or SMTP_PASSWORD environment variables missing.")
        return

    count = len(scored_listings)
    subject = (
        f"🏠 SF Scout: {count} New Match{'es' if count != 1 else ''} "
        f"(Top Score: {scored_listings[0].total_score:.0f})"
    )

    html_body = _build_html_email(scored_listings)

    # Build MIME message
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"SF Apartment Scout <{SMTP_EMAIL}>"
    msg["To"] = ", ".join(RECIPIENT_EMAILS)

    # Attach HTML content
    html_part = MIMEText(html_body, "html", "utf-8")
    msg.attach(html_part)

    try:
        logger.info("Connecting to SMTP server %s:%d...", SMTP_HOST, SMTP_PORT)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.sendmail(SMTP_EMAIL, RECIPIENT_EMAILS, msg.as_string())

        logger.info(
            "Email dispatched successfully to %d recipient(s): %s",
            len(RECIPIENT_EMAILS),
            ", ".join(RECIPIENT_EMAILS),
        )
    except Exception as exc:
        logger.error("Failed to send email via SMTP: %s", exc)
        raise
