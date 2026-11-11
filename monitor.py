#!/usr/bin/env python3
"""Watch the King's Residences portal for Moonraker Point and email when it appears.

Flow (read-only, never creates a room lock or booking):
  login -> "Book Your Room" -> Next (welcome) -> Next (booking terms) -> "Select your room"
On that page, read the Room Type list and the "Available Rooms by Residences" counts.

Secrets live in macOS Keychain, never in this folder:
  service "kcl-monitor-portal"  account = KCL ID (no K)   -> portal password
  service "kcl-monitor-gmail"   account = Gmail address    -> Gmail app password
"""
import json
import os
import re
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
_cfg_file = HERE / "config.json"
CONFIG = json.loads(_cfg_file.read_text()) if _cfg_file.exists() else {}
# In the cloud (GitHub Actions) everything personal comes from encrypted secrets.
for key, env in [("kcl_id", "KCL_ID"), ("gmail_address", "GMAIL_ADDRESS"), ("alert_to", "ALERT_TO")]:
    if os.environ.get(env):
        CONFIG[key] = os.environ[env]
STATE_FILE = HERE / os.environ.get("STATE_FILE", "state.json")
LOG_FILE = HERE / "monitor.log"

PORTAL = "https://accommodation.kcl.ac.uk/"
# keyword to match in the portal -> name used in the alert email
TARGETS = {
    "Moonraker": "Moonraker Point",
    "Stamford": "Stamford Street Apartments",
}
FAIL_ALERT_AFTER = 3  # consecutive failures before a "monitor is broken" email


def log(msg):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC  {msg}"
    print(line)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def keychain(service, account):
    env = {"kcl-monitor-portal": "KCL_PORTAL_PASSWORD", "kcl-monitor-gmail": "GMAIL_APP_PASSWORD"}[service]
    if os.environ.get(env):
        return os.environ[env].strip()
    return subprocess.run(
        ["/usr/bin/security", "find-generic-password", "-s", service, "-a", account, "-w"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def send_email(subject, body):
    gmail = CONFIG["gmail_address"]
    msg = EmailMessage()
    msg["From"] = gmail
    msg["To"] = CONFIG.get("alert_to") or gmail
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(gmail, keychain("kcl-monitor-gmail", gmail).replace(" ", ""))
        s.send_message(msg)
    log(f"email sent: {subject}")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"target_seen": False, "failures": 0, "fail_alerted": False}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def scrape():
    """Return (room_types, residences) currently listed as bookable."""
    kcl_id = CONFIG["kcl_id"]
    password = keychain("kcl-monitor-portal", kcl_id)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1328, "height": 1200})
        page = ctx.new_page()
        page.set_default_timeout(45_000)

        page.goto(PORTAL)
        page.fill("[rmsmodel=UserNameEntry]", kcl_id)
        page.fill("[rmsmodel=PasswordEntry]", password)
        page.click("#LoginButton")
        book = page.locator("input[value='Book Your Room']")
        book.wait_for()

        # "Book Your Room" opens the booking flow in a popup window.
        with ctx.expect_page() as popup_info:
            book.click()
        flow = popup_info.value
        flow.set_default_timeout(45_000)
        flow.wait_for_load_state()

        # Click Next until we reach the room search page (welcome -> terms -> search).
        # Pages swap in place via AJAX, so wait for the body text to change after each click.
        for _ in range(5):
            flow.wait_for_timeout(2000)
            before = flow.inner_text("body")
            if "Search for rooms" in before:
                break
            flow.locator("#NavButtonNext").click()
            try:
                flow.wait_for_function(
                    "prev => document.body.innerText !== prev", arg=before, timeout=30_000
                )
            except Exception:
                pass  # re-check on the next pass
        else:
            raise RuntimeError("never reached 'Search for rooms' page; portal flow may have changed")

        flow.get_by_text("Available Rooms by Residences").wait_for()
        room_types = [
            t.strip()
            for t in flow.locator("select option").all_inner_texts()
            if t.strip()
        ]
        body = flow.inner_text("body")
        residences = re.findall(r"Residence:\s*([^\n(]+?)\s*\((\d+)\)", body)
        browser.close()
    return room_types, [(n.strip(), int(c)) for n, c in residences], body


def main():
    state = load_state()
    try:
        room_types, residences, body = scrape()
    except Exception as e:
        state["failures"] = state.get("failures", 0) + 1
        log(f"FAILED ({state['failures']} in a row): {e!r}")
        if state["failures"] >= FAIL_ALERT_AFTER and not state.get("fail_alerted"):
            try:
                send_email(
                    "KCL room monitor is failing",
                    f"The KCL residence monitor has failed {state['failures']} times in a row.\n"
                    f"Last error: {e!r}\n\nCheck {LOG_FILE}. Your portal password may have "
                    "changed, or the portal layout changed.",
                )
                state["fail_alerted"] = True
            except Exception as mail_err:
                log(f"could not send failure email: {mail_err!r}")
        save_state(state)
        return 1

    state["failures"] = 0
    state["fail_alerted"] = False
    summary = ", ".join(f"{n} ({c})" for n, c in residences) or "none listed"
    seen = state.setdefault("seen", {})
    status = []
    for keyword, name in TARGETS.items():
        k = keyword.lower()
        hits = [t for t in room_types if k in t.lower()] + [
            f"{n} ({c} rooms)" for n, c in residences if k in n.lower()
        ]
        if not hits and k in body.lower():
            hits = [f"'{keyword}' mentioned on the room search page"]
        status.append(f"{keyword}: {'YES ' + str(hits) if hits else 'no'}")

        if hits and not seen.get(keyword):
            send_email(
                f"🚨 {name} is bookable on the KCL portal",
                f"{name} just appeared in the King's Residences portal.\n\n"
                + "\n".join(f"  • {h}" for h in hits)
                + f"\n\nAll residences listed right now: {summary}\n\n"
                "Book now: https://accommodation.kcl.ac.uk/ -> Book Your Room -> Next -> Next -> "
                f"choose a {keyword} room type and Search.\n"
                f"Checked at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC.",
            )
            seen[keyword] = True
        elif not hits and seen.get(keyword):
            log(f"{name} no longer listed; will alert again if it reappears")
            seen[keyword] = False

    log(f"residences: {summary} | " + " | ".join(status))

    state["last_residences"] = residences
    save_state(state)
    return 0


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        send_email("KCL room monitor: test email", "If you can read this, alerts will reach you.")
        sys.exit(0)
    sys.exit(main())
