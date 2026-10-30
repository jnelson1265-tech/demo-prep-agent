"""Demo Call Prep Agent.

Takes a demo booking, reads the prospect's website, and writes a prep brief.

Usage:
    python prep_brief.py --website "example.com" --fetch-only
    python prep_brief.py --sample bookings/sample1.json --prompt v1 --dry-run
    python prep_brief.py --name "..." --email "..." --website "..." --note "..." --dry-run
    python prep_brief.py --watch            (check the test Calendly account every 2 minutes)
    python prep_brief.py --watch --once     (check one time)
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent

MAX_CHARS = 6000          # total site text sent to Claude later
ABOUT_MAX_CHARS = 2500    # share of that reserved for an About page
MIN_USEFUL_CHARS = 200    # below this, the site is probably JavaScript-only
TIMEOUT = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

SOCIAL_DOMAINS = (
    "instagram.com", "facebook.com", "tiktok.com", "linkedin.com",
    "x.com", "twitter.com", "youtube.com", "linktr.ee", "pinterest.com",
)


# ---------- Step 1: clean the URL ----------

def clean_url(raw):
    """Turn whatever the prospect typed into a usable URL.

    Returns (url, problem). url is None if it can't be used.
    """
    if not raw or not raw.strip():
        return None, "No website given"

    text = raw.strip().strip("<>\"'").replace(" ", "")

    if text.startswith("@"):
        return None, f"Looks like a social handle, not a website: {raw}"

    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text

    parsed = urlparse(text)
    host = parsed.netloc.lower()

    if "." not in host:
        return None, f"Doesn't look like a website: {raw}"

    if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
        return text, f"Social profile, not a website: {host}"

    return text, None


# ---------- Step 2: fetch and clean the page ----------

def page_text(html):
    """Strip navigation, scripts, and layout, and return title, meta description, and body text."""
    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    meta_description = meta.get("content", "").strip() if meta else ""

    for tag in soup(["script", "style", "noscript", "svg", "nav", "header",
                     "footer", "form", "iframe", "button"]):
        tag.decompose()

    lines = []
    for line in soup.get_text("\n").splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) > 2 and line not in lines:   # drop blanks and repeated lines
            lines.append(line)

    return title, meta_description, "\n".join(lines), soup


def find_about_link(html, base_url):
    """Return the first same-site link that looks like an About page."""
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        label = a.get_text(" ", strip=True).lower()
        if "about" in href or label.startswith("about"):
            full = urljoin(base_url, a["href"])
            if urlparse(full).netloc == base_host and full.rstrip("/") != base_url.rstrip("/"):
                return full
    return None


def fetch_site(url):
    """Fetch the homepage (and an About page if one is linked).

    Always returns a dict. If something goes wrong, 'error' explains it
    and the rest of the agent works from the prep note only.
    """
    result = {"url": url, "final_url": None, "title": "", "meta_description": "",
              "text": "", "pages": [], "error": None}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        result["error"] = f"Couldn't load the site: {e.__class__.__name__}"
        return result

    result["final_url"] = resp.url
    title, meta, text, _ = page_text(resp.text)
    result.update(title=title, meta_description=meta)
    result["pages"].append(resp.url)

    about_text = ""
    about_url = find_about_link(resp.text, resp.url)
    if about_url:
        try:
            about = requests.get(about_url, headers=HEADERS, timeout=TIMEOUT)
            about.raise_for_status()
            about_text = page_text(about.text)[2][:ABOUT_MAX_CHARS]
            result["pages"].append(about.url)
        except requests.RequestException:
            pass  # the About page is optional

    home_budget = MAX_CHARS - len(about_text)
    combined = text[:home_budget]
    if about_text:
        combined += "\n\n--- About page ---\n" + about_text
    result["text"] = combined

    if len(text) < MIN_USEFUL_CHARS and len(about_text) < MIN_USEFUL_CHARS:
        result["error"] = "Very little readable text (site may be built with JavaScript)"

    return result


# ---------- Step 3: ask Claude ----------

def load_creds():
    try:
        with open(HERE / "creds.json") as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit("creds.json not found. Copy creds.example.json to creds.json and fill it in.")


def build_prompt(version, booking, site, url_problem):
    template = (HERE / "prompts" / f"{version}.txt").read_text()

    site_text = "(no website text available)"
    if site and (site["text"] or site["title"]):
        header = f"Page title: {site['title']}\nMeta description: {site['meta_description']}\n\n"
        site_text = header + site["text"]
    problems = [p for p in (url_problem, site and site["error"]) if p]
    if problems:
        site_text = "WEBSITE PROBLEM: " + "; ".join(problems) + "\n\n" + site_text

    # One template per line, no indentation: same content, fewer tokens.
    library = json.loads((HERE / "templates.json").read_text())
    templates = "\n".join(json.dumps(t, ensure_ascii=False) for t in library)

    values = {
        "name": booking["name"],
        "email": booking["email"],
        "website": booking["website"],
        "note": booking.get("note") or "(no note)",
        "site_text": site_text,
        "templates": templates,
    }
    # plain replace, so braces in the site text or JSON don't break anything
    for key, value in values.items():
        template = template.replace("{" + key + "}", value)
    return template


def ask_claude(prompt, creds):
    client = anthropic.Anthropic(api_key=creds["anthropic_api_key"])
    response = client.messages.create(
        model=creds.get("claude_model") or "claude-sonnet-5-5",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    # The response can include thinking blocks before the answer; keep only the text.
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text:
        sys.exit(f"Claude returned no text (stop reason: {response.stop_reason}).")
    u = response.usage
    print(f"Tokens:  {u.input_tokens} in, {u.output_tokens} out (limit {8000})")
    return text


# ---------- Step 4: check the brief ----------

def parse_json(text):
    """Pull the JSON object out of Claude's reply (it may wrap it in ``` fences)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def normalize(s):
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"[^a-z0-9%$' ]+", " ", re.sub(r"\s+", " ", s)).split()


def quote_found(quote, source):
    """True if the quote really appears in the site text or note (ignoring case and punctuation)."""
    q = " ".join(normalize(quote))
    return bool(q) and q in " ".join(normalize(source))


def check_brief(brief, site, booking):
    """Drop templates that aren't in the library and flag evidence that isn't on the site.

    Returns a list of warnings (also shown in the brief).
    """
    warnings = []
    source = (site["title"] + "\n" + site["meta_description"] + "\n" + site["text"]) if site else ""
    source += "\n" + (booking.get("note") or "")

    library = {t["url"].rstrip("/"): t for t in json.loads((HERE / "templates.json").read_text())}
    kept = []
    for t in brief.get("templates", []):
        real = library.get((t.get("url") or "").rstrip("/"))
        if real:
            kept.append({**t, "name": real["name"], "url": real["url"]})
        else:
            warnings.append(f"Removed a template that isn't in the library: {t.get('name')}")
    brief["templates"] = kept

    items = [("What they do", w) for w in brief.get("what_they_do", [])]
    if brief.get("audience", {}).get("evidence"):
        items.append(("Audience", brief["audience"]))
    for label, item in items:
        if not quote_found(item.get("evidence", ""), source):
            item["unverified"] = True
            warnings.append(f"{label}: quote not found on the site: \"{item.get('evidence')}\"")
    for q in brief.get("questions", []):
        if q.get("based_on") and not quote_found(q["based_on"], source):
            q["unverified"] = True
            warnings.append(f"Question based on a quote not found on the site: \"{q['based_on']}\"")

    # Quotes inside a template's or quiz idea's "why" must be real too.
    for t in brief["templates"] + brief.get("quiz_ideas", []):
        for quoted in re.findall(r"[\"“]([^\"”]{4,})[\"”]", t.get("why", "")):
            if not quote_found(quoted, source):
                t["unverified"] = True
                label = t.get("name") or t.get("title")
                warnings.append(f"\"{label}\": quote not found on the site: \"{quoted}\"")

    return warnings


def shows_quotes(version):
    """v2 and v3 show each quote under its claim. From v4 on, quotes are only
    used for checking, and only shown when a check fails."""
    return version in ("v2", "v3")


def format_brief(brief, booking, url, warnings, version="v4"):
    """Plain-text version of the brief for the terminal."""
    show = shows_quotes(version)

    def quote(item, key, label=""):
        if item.get("unverified"):
            return f"\n    {label}\"{item.get(key, '')}\" (UNVERIFIED)"
        return f"\n    {label}\"{item[key]}\"" if show and item.get(key) else ""

    lines = [f"PROSPECT: {booking['name']} | {booking['email']} | {url or booking['website']}",
             f"THEIR NOTE: {booking.get('note') or 'No note from the prospect'}", ""]

    lines.append("WHAT THEY DO")
    for w in brief.get("what_they_do", []):
        lines.append(f"- {w['claim']}{quote(w, 'evidence')}")

    a = brief.get("audience", {})
    lines += ["", "AUDIENCE", f"- {a.get('claim', 'Unknown')}{quote(a, 'evidence')}"]

    lines += ["", "RECOMMENDED TEMPLATES"]
    for t in brief.get("templates", []):
        mark = " (UNVERIFIED)" if t.get("unverified") else ""
        lines.append(f"- {t['name']}\n    {t['url']}\n    Why: {t['why']}{mark}")
    if not brief.get("templates"):
        lines.append("- No close match in the template library")

    if brief.get("quiz_ideas"):
        lines += ["", "QUIZ IDEAS"]
        for idea in brief["quiz_ideas"]:
            mark = " (UNVERIFIED)" if idea.get("unverified") else ""
            lines.append(f"- {idea['title']}\n    Why: {idea['why']}{mark}")

    lines += ["", "DISCOVERY QUESTIONS"]
    for i, q in enumerate(brief.get("questions", []), 1):
        lines.append(f"{i}. {q['question']}{quote(q, 'based_on', 'Based on: ')}")

    flags = brief.get("flags", []) + warnings
    if flags:
        lines += ["", "FLAGS"] + [f"- {f}" for f in flags]
    return "\n".join(lines)


# ---------- Step 5: post to Slack ----------

def esc(s):
    """Slack needs these three characters escaped."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def slack_blocks(brief, booking, url, warnings, version):
    site_link = f"<{url}|{esc(urlparse(url).netloc)}>" if url else esc(booking["website"])
    note = booking.get("note")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": f"Demo prep: {booking['name']}"[:150]}},
        section(f"*Prospect:* {esc(booking['name'])} · {esc(booking['email'])} · {site_link}\n"
                f"*Their note:* {'_' + esc(note) + '_' if note else 'No note from the prospect'}"),
        {"type": "divider"},
    ]

    show = shows_quotes(version)

    def quoted(item, key):
        if item.get("unverified"):
            return f"\n>“{esc(item.get(key, ''))}”  :warning: _quote not found on their site_"
        return f"\n>“{esc(item[key])}”" if show and item.get(key) else ""

    what = "\n".join(f"• {esc(w['claim'])}{quoted(w, 'evidence')}" for w in brief.get("what_they_do", []))
    blocks.append(section("*What they do*\n" + (what or "Unknown")))

    a = brief.get("audience", {})
    blocks.append(section(f"*Audience*\n• {esc(a.get('claim', 'Unknown'))}{quoted(a, 'evidence')}"))

    if brief.get("templates"):
        lines = []
        for t in brief["templates"]:
            mark = "  :warning: _unverified quote_" if t.get("unverified") else ""
            lines.append(f"• <{t['url']}|{esc(t['name'])}>\n    {esc(t['why'])}{mark}")
        blocks.append(section("*Recommended templates*\n" + "\n".join(lines)))
    else:
        blocks.append(section("*Recommended templates*\nNo close match in the template library"))

    if brief.get("quiz_ideas"):
        lines = []
        for idea in brief["quiz_ideas"]:
            mark = "  :warning: _unverified quote_" if idea.get("unverified") else ""
            lines.append(f"• *{esc(idea['title'])}*\n    {esc(idea['why'])}{mark}")
        blocks.append(section("*Quiz ideas*\n" + "\n".join(lines)))

    qs = "\n".join(f"{i}. {esc(q['question'])}{quoted(q, 'based_on')}"
                   for i, q in enumerate(brief.get("questions", []), 1))
    blocks.append(section("*Discovery questions*\n" + (qs or "None")))

    flags = brief.get("flags", []) + warnings
    if flags:
        blocks.append(section("*Flags*\n" + "\n".join(f":warning: {esc(f)}" for f in flags)))

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"Prepared by the demo prep agent · prompt {version}"}]})
    return blocks


def raw_blocks(text, booking, version):
    """For v1, which returns free text: post it as is, split into chunks Slack accepts."""
    blocks = [{"type": "header", "text": {"type": "plain_text",
                                          "text": f"Demo prep: {booking['name']}"[:150]}}]
    for i in range(0, min(len(text), 2900 * 45), 2900):
        blocks.append(section(esc(text[i:i + 2900])))
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
                   "text": f"Prepared by the demo prep agent · prompt {version}"}]})
    return blocks


def post_to_slack(blocks, fallback_text, creds):
    webhook = creds.get("slack_webhook_url")
    if not webhook:
        print("No slack_webhook_url in creds.json, so nothing was posted.")
        return False
    resp = requests.post(webhook, json={"text": fallback_text, "blocks": blocks}, timeout=TIMEOUT)
    if resp.status_code != 200:
        print(f"Slack rejected the post ({resp.status_code}): {resp.text}")
        return False
    print("Posted to Slack.")
    return True


def save_output(version, url, text):
    """Keep every run so prompt versions can be compared later."""
    out_dir = HERE / "outputs"
    out_dir.mkdir(exist_ok=True)
    host = urlparse(url).netloc.replace("www.", "") if url else "no-site"
    path = out_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{version}_{host}.md"
    path.write_text(text)
    return path


# ---------- Step 6: watch the test Calendly account ----------

CALENDLY_API = "https://api.calendly.com"
SEEN_FILE = HERE / "seen_bookings.json"


def calendly_get(path_or_url, token, params=None):
    url = path_or_url if path_or_url.startswith("http") else CALENDLY_API + path_or_url
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                        params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def booking_from_invitee(invitee):
    """Map a Calendly invitee to our booking fields using the form's questions."""
    booking = {"name": invitee.get("name", ""), "email": invitee.get("email", ""),
               "website": "", "note": ""}
    for qa in invitee.get("questions_and_answers", []):
        question = qa.get("question", "").lower()
        answer = (qa.get("answer") or "").strip()
        if "website" in question or "url" in question:
            booking["website"] = answer
        elif answer and not booking["note"]:
            booking["note"] = answer   # the optional "anything that will help us prepare" box
    return booking


def new_bookings(token):
    """Return (invitee_uri, booking) for upcoming bookings we haven't handled yet."""
    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    user_uri = calendly_get("/users/me", token)["resource"]["uri"]
    events = calendly_get("/scheduled_events", token, params={
        "user": user_uri, "status": "active", "count": 50,
        "min_start_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })["collection"]

    found = []
    for event in events:
        invitees = calendly_get(event["uri"] + "/invitees", token,
                                params={"status": "active"})["collection"]
        for inv in invitees:
            if inv["uri"] not in seen:
                found.append((inv["uri"], booking_from_invitee(inv)))
    return found


def mark_seen(invitee_uri):
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    seen.append(invitee_uri)
    SEEN_FILE.write_text(json.dumps(seen, indent=2))


def watch(args, creds):
    token = creds.get("calendly_token")
    if not token:
        sys.exit("No calendly_token in creds.json.")
    print(f"Watching Calendly for new bookings (every {args.interval} seconds). Ctrl+C to stop.")
    while True:
        try:
            found = new_bookings(token)
        except requests.RequestException as e:
            print(f"Calendly check failed: {e}")
            found = []
        for invitee_uri, booking in found:
            print(f"\nNew booking: {booking['name']} ({booking['email']})")
            if not booking["website"]:
                booking["website"] = "(none given)"
            try:
                process_booking(booking, args, creds)
            except Exception as e:   # one bad booking shouldn't stop the watcher
                print(f"Couldn't prepare this booking: {e}")
                continue
            mark_seen(invitee_uri)
        if args.once:
            if not found:
                print("No new bookings.")
            return
        time.sleep(args.interval)


# ---------- Run one booking ----------

def process_booking(booking, args, creds=None, saved_path=None):
    url, problem = clean_url(booking["website"])
    print(f"Input:   {booking['website']}")
    print(f"Cleaned: {url}")
    if problem:
        print(f"Warning: {problem}")

    site = fetch_site(url) if url else None
    if site:
        print(f"Fetched: {', '.join(site['pages']) or 'nothing'}")
        print(f"Length:  {len(site['text'])} characters")
        if site["error"]:
            print(f"Problem: {site['error']}")

    if args.fetch_only:
        if site:
            print(f"Title:   {site['title']}")
            print(f"Meta:    {site['meta_description']}")
            print("\n" + "=" * 60 + "\n")
            print(site["text"])
        return

    creds = creds or load_creds()
    if saved_path:
        saved_text = saved_path.read_text()
        marker = "\n---\nRaw reply:\n"
        raw = saved_text.split(marker, 1)[1] if marker in saved_text else saved_text
        print(f"Using saved run: {saved_path.name} (prompt {args.prompt})\n")
    else:
        prompt = build_prompt(args.prompt, booking, site, problem)
        print(f"Prompt:  {args.prompt} ({len(prompt)} characters)")
        print("Asking Claude...\n")
        raw = ask_claude(prompt, creds)

    # v1 returns free text; v2 and later return JSON we can check.
    data = parse_json(raw) if args.prompt != "v1" else None
    warnings = []
    if args.prompt != "v1" and data is None:
        print("Claude didn't return valid JSON. Raw reply:\n")
        brief = raw
    elif data is not None:
        warnings = check_brief(data, site, booking)
        brief = format_brief(data, booking, url, warnings, args.prompt)
        for w in warnings:
            print(f"Check:   {w}")
    else:
        brief = raw

    print("=" * 60 + "\n")
    print(brief)
    print("\n" + "=" * 60)
    if not saved_path:
        saved = save_output(args.prompt, url,
                            brief if data is None else brief + "\n\n---\nRaw reply:\n" + raw)
        print(f"Saved to {saved.relative_to(HERE)}")
    if args.dry_run:
        print("Dry run, so nothing was posted to Slack.")
        return
    if data is not None:
        blocks = slack_blocks(data, booking, url, warnings, args.prompt)
    else:
        blocks = raw_blocks(raw, booking, args.prompt)
    post_to_slack(blocks, f"Demo prep: {booking['name']}", creds)


# ---------- Command line ----------

def main():
    parser = argparse.ArgumentParser(description="Demo Call Prep Agent")
    parser.add_argument("--name")
    parser.add_argument("--email")
    parser.add_argument("--website", help="Prospect's website as they typed it")
    parser.add_argument("--note", default="")
    parser.add_argument("--sample", help="Path to a saved booking, e.g. bookings/sample1.json")
    parser.add_argument("--prompt", default="v4", help="Prompt version in prompts/, e.g. v1")
    parser.add_argument("--fetch-only", action="store_true",
                        help="Just fetch the site and print the text")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the brief instead of posting to Slack")
    parser.add_argument("--from-output",
                        help="Post a saved run from outputs/ to Slack without calling Claude again")
    parser.add_argument("--watch", action="store_true",
                        help="Check the test Calendly account for new bookings and prep each one")
    parser.add_argument("--once", action="store_true",
                        help="With --watch: check one time and stop")
    parser.add_argument("--interval", type=int, default=120,
                        help="With --watch: seconds between checks (default 120)")
    args = parser.parse_args()

    if args.watch:
        watch(args, load_creds())
        return

    saved_path = None
    if args.from_output:
        saved_path = Path(args.from_output)
        match = re.search(r"_(v\d+)_", saved_path.name)
        args.prompt = match.group(1) if match else args.prompt

    if args.sample:
        booking = json.loads(Path(args.sample).read_text())
    else:
        booking = {"name": args.name, "email": args.email,
                   "website": args.website, "note": args.note}
    if not booking.get("website"):
        sys.exit("A website is required (--website or --sample).")
    if not args.fetch_only and (not booking.get("name") or not booking.get("email")):
        sys.exit("Name and email are required (--name/--email or --sample).")

    process_booking(booking, args, saved_path=saved_path)


if __name__ == "__main__":
    main()
