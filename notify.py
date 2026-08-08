#!/usr/bin/env python3
"""
Notification channels for showwatch. Stdlib only.

Channels configure themselves from environment variables, and every one that
is configured gets the alert. Nothing to edit in code: set the variables for
the channel you want (as GitHub Actions secrets, for a cron deploy) and it
switches itself on.

  slack     SLACK_WEBHOOK_URL
  discord   DISCORD_WEBHOOK_URL
  telegram  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  ntfy      NTFY_TOPIC            (optional NTFY_SERVER, default ntfy.sh)
  pushover  PUSHOVER_USER_KEY + PUSHOVER_APP_TOKEN
  email     SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO
            (optional SMTP_PORT, default 587)
  webhook   GENERIC_WEBHOOK_URL   posts {"text": ...} as JSON
  github    GITHUB_TOKEN + GITHUB_REPOSITORY   opens an issue

A note on what actually wakes you up: Telegram, ntfy and Pushover push to a
phone lock screen. Email does not, reliably. GitHub issues rely on the GitHub
app's notifications. Pick accordingly - an alert you don't see is not an alert.
"""

import json
import os
import urllib.request


def _post_json(url, payload, headers=None, timeout=20):
    head = {"Content-Type": "application/json"}
    head.update(headers or {})
    req = urllib.request.Request(url, json.dumps(payload).encode(), head)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# --------------------------------------------------------------------------
# Channels. Each returns True if it sent, False if it isn't configured.
# --------------------------------------------------------------------------


def _slack(title, body, url):
    hook = os.environ.get("SLACK_WEBHOOK_URL")
    if not hook:
        return False
    text = "*%s*\n%s" % (title, body)
    if url:
        text += "\n<%s|Book now>" % url
    _post_json(hook, {"text": text})
    return True


def _discord(title, body, url):
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not hook:
        return False
    text = "**%s**\n%s" % (title, body)
    if url:
        text += "\n%s" % url
    _post_json(hook, {"content": text[:1900]})
    return True


def _telegram(title, body, url):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    text = "*%s*\n%s" % (title, body)
    if url:
        text += "\n%s" % url
    _post_json(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        {"chat_id": chat, "text": text, "parse_mode": "Markdown"},
    )
    return True


def _ntfy(title, body, url):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        # High priority so it breaks through a silenced phone - the whole
        # point is catching a window that closes in minutes.
        "priority": 5,
        "tags": ["ticket"],
    }
    if url:
        payload["actions"] = [{"action": "view", "label": "Book now", "url": url}]
    _post_json(server, payload)
    return True


def _pushover(title, body, url):
    user = os.environ.get("PUSHOVER_USER_KEY")
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    if not (user and token):
        return False
    payload = {
        "token": token,
        "user": user,
        "title": title,
        "message": body,
        "priority": 1,
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "Book now"
    _post_json("https://api.pushover.net/1/messages.json", payload)
    return True


def _email(title, body, url):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO")
    if not (host and user and password and to):
        return False

    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = user
    msg["To"] = to
    msg.set_content(body + (("\n\n" + url) if url else ""))

    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", 587)), timeout=30) as smtp:
        smtp.starttls()
        smtp.login(user, password)
        smtp.send_message(msg)
    return True


def _webhook(title, body, url):
    hook = os.environ.get("GENERIC_WEBHOOK_URL")
    if not hook:
        return False
    _post_json(hook, {"title": title, "text": body, "url": url})
    return True


def _github_issue(title, body, url):
    """Zero extra accounts: open an issue on the repo the cron already runs in."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (token and repo):
        return False
    _post_json(
        "https://api.github.com/repos/%s/issues" % repo,
        {"title": title, "body": body + (("\n\n%s" % url) if url else "")},
        {
            "Authorization": "Bearer %s" % token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "showwatch",
        },
    )
    return True


CHANNELS = [
    ("slack", _slack),
    ("discord", _discord),
    ("telegram", _telegram),
    ("ntfy", _ntfy),
    ("pushover", _pushover),
    ("email", _email),
    ("webhook", _webhook),
    ("github", _github_issue),
]


def _is_configured(name):
    need = {
        "slack": ["SLACK_WEBHOOK_URL"],
        "discord": ["DISCORD_WEBHOOK_URL"],
        "telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
        "ntfy": ["NTFY_TOPIC"],
        "pushover": ["PUSHOVER_USER_KEY", "PUSHOVER_APP_TOKEN"],
        "email": ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"],
        "webhook": ["GENERIC_WEBHOOK_URL"],
        "github": ["GITHUB_TOKEN", "GITHUB_REPOSITORY"],
    }[name]
    return all(os.environ.get(k) for k in need)


def configured():
    """Names of the channels that have their environment variables set."""
    return [name for name, _ in CHANNELS if _is_configured(name)]


def send(title, body, url=""):
    """Send to every configured channel. Returns (sent_to, failures).

    Delivery to at least one channel is what lets the caller advance state. An
    alert that reached nobody must not be treated as delivered - the opening it
    describes is reported once and only once.
    """
    sent, failed = [], []
    for name, fn in CHANNELS:
        if not _is_configured(name):
            continue
        try:
            if fn(title, body, url):
                sent.append(name)
        except Exception as exc:
            failed.append("%s: %s" % (name, exc))
    return sent, failed
