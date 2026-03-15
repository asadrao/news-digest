#!/usr/bin/env python3
"""News digest agent: fetches articles via NewsAPI, curates with Claude, emails via SendGrid."""

import json
import os
from datetime import datetime, timedelta, timezone

import anthropic
import requests
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

# ── Configuration ──────────────────────────────────────────────────────────────
NEWS_TOPIC = os.environ.get("NEWS_TOPIC", "artificial intelligence")
NUM_ARTICLES = int(os.environ.get("NUM_ARTICLES", "5"))
EMAIL_FROM = os.environ["EMAIL_FROM"]
EMAIL_TO = os.environ["EMAIL_TO"]
NEWS_API_KEY = os.environ["NEWS_API_KEY"]
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
# ANTHROPIC_API_KEY is read automatically by the Anthropic SDK


def fetch_articles(topic: str, max_results: int = 30) -> list[dict]:
    """Fetch recent articles about *topic* from NewsAPI (past 7 days).

    NewsAPI free tier has a ~24 hour indexing delay, so searching only the
    past day returns nothing. 7 days gives a reliable window of results.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": topic,
            "from": since,
            "sortBy": "relevancy",
            "pageSize": max_results,
            "language": "en",
            "apiKey": NEWS_API_KEY,
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"NewsAPI error: {data.get('message', data)}")
    raw = data.get("articles", [])
    print(f"NewsAPI returned {data.get('totalResults', '?')} total results, fetched {len(raw)}")
    return [
        {
            "title": a["title"],
            "description": a.get("description") or "(no description)",
            "url": a["url"],
            "source": a["source"]["name"],
            "publishedAt": a["publishedAt"],
        }
        for a in raw
        if a.get("title") and a.get("url") and a.get("title") != "[Removed]"
    ]


def curate_articles(articles: list[dict], topic: str, count: int) -> list[dict]:
    """Ask Claude to pick the top *count* articles and write summaries."""
    client = anthropic.Anthropic()

    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=2048,
        system=(
            "You are a sharp news curator. "
            "Return only valid JSON — no markdown fences, no prose before or after."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f'Select the {count} most relevant and interesting articles about "{topic}" '
                    f"from the list below. Prefer substantive reporting over opinion or clickbait.\n\n"
                    f"For each selected article, write a 2-3 sentence summary that captures the key insight "
                    f"and why it matters.\n\n"
                    f"Return a JSON array with this exact shape:\n"
                    f'[{{"title":"...","summary":"...","url":"...","source":"...","publishedAt":"..."}}]\n\n'
                    f"Articles to evaluate:\n{json.dumps(articles, indent=2)}"
                ),
            }
        ],
    )

    text = next(b.text for b in response.content if b.type == "text").strip()

    # Strip accidental markdown code fences
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.rsplit("```", 1)[0].strip()

    return json.loads(text)


def build_html(articles: list[dict], topic: str, date_str: str) -> str:
    """Render the digest as a clean HTML email."""
    items = ""
    for a in articles:
        published = a.get("publishedAt", "")[:10]
        items += f"""
        <div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e5e7eb">
          <p style="margin:0 0 4px;font-size:12px;color:#6b7280">{a['source']} · {published}</p>
          <h2 style="margin:0 0 8px;font-size:18px;line-height:1.4;color:#111827">
            <a href="{a['url']}" style="color:#2563eb;text-decoration:none">{a['title']}</a>
          </h2>
          <p style="margin:0;font-size:15px;line-height:1.6;color:#374151">{a['summary']}</p>
          <p style="margin:8px 0 0">
            <a href="{a['url']}" style="font-size:13px;color:#2563eb">Read full article →</a>
          </p>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;padding:0;margin:0">
  <div style="max-width:640px;margin:32px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.12)">
    <div style="background:#1e40af;padding:28px 32px">
      <p style="margin:0 0 4px;font-size:11px;color:#93c5fd;text-transform:uppercase;letter-spacing:.1em">Daily Digest</p>
      <h1 style="margin:0;font-size:26px;color:#fff">{topic.title()}</h1>
      <p style="margin:6px 0 0;font-size:14px;color:#bfdbfe">{date_str}</p>
    </div>
    <div style="padding:32px">
      {items}
      <p style="margin:24px 0 0;font-size:12px;color:#9ca3af;text-align:center">
        Curated by Claude · Powered by NewsAPI &amp; SendGrid
      </p>
    </div>
  </div>
</body>
</html>"""


def send_email(html: str, topic: str, date_str: str) -> None:
    """Send the digest via SendGrid."""
    message = Mail(
        from_email=EMAIL_FROM,
        to_emails=EMAIL_TO,
        subject=f"News Digest: {topic.title()} — {date_str}",
        html_content=html,
    )
    sg = SendGridAPIClient(SENDGRID_API_KEY)
    response = sg.send(message)
    print(f"Email sent: HTTP {response.status_code}")


def main() -> None:
    now = datetime.now(timezone.utc)
    date_str = f"{now.strftime('%B')} {now.day}, {now.year}"

    print(f"Topic: {NEWS_TOPIC!r}")
    articles = fetch_articles(NEWS_TOPIC)
    print(f"Fetched {len(articles)} articles from NewsAPI")

    if not articles:
        print("No articles found — skipping email.")
        return

    print(f"Curating top {NUM_ARTICLES} articles with Claude...")
    curated = curate_articles(articles, NEWS_TOPIC, NUM_ARTICLES)
    print(f"Selected {len(curated)} articles")

    html = build_html(curated, NEWS_TOPIC, date_str)
    send_email(html, NEWS_TOPIC, date_str)


if __name__ == "__main__":
    main()
