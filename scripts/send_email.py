#!/usr/bin/env python3
"""Send a rendered briefing by email through AgentMail.

  python send_email.py --out DIR [--to addr ...] [--dry-run] [--force]

Reads the sender inbox and recipients from config/services.yaml (distribution),
and the API key from the AGENTMAIL_API_KEY environment variable. The body is
email.html (summary, colour grid, key times, synopsis); briefing.pdf is attached.

Refuses to send sample data, or a narrative with outstanding language flags
(override the latter with --force only after a human has read it).
"""

from __future__ import annotations

import argparse
import base64
import html as _html
import json
import os
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--to", nargs="*", help="override recipients (e.g. a test address)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="send despite language flags")
    a = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "config" / "services.yaml").read_text())["distribution"]
    out = pathlib.Path(a.out)
    data = json.loads((out / "data.json").read_text())
    if data.get("sample"):
        sys.exit("refusing: this is SAMPLE data")
    flags = (out / "language_flags.txt").read_text().strip() if (out / "language_flags.txt").exists() else ""
    if flags and not a.force:
        sys.exit("refusing: language flags outstanding — fix narrative.json and re-render:\n" + flags)
    pdf = out / "briefing.pdf"
    if not pdf.exists():
        sys.exit("refusing: briefing.pdf missing — run render first")

    to = a.to or cfg["recipients"]
    subject = (out / "email_subject.txt").read_text().strip()
    html = (out / "email.html").read_text()
    text = re.sub(r"\n\s*\n+", "\n\n", _html.unescape(re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>|</(div|p|tr)>", "\n", html))))
    fname = f"Marine-Weather-Briefing-{data['generated_local'][:10]}.pdf"
    print(f"from {cfg['from_inbox']} to {to}\nsubject: {subject}\nattachment: {fname} ({pdf.stat().st_size:,} bytes)")
    if a.dry_run:
        return

    key = os.environ.get("AGENTMAIL_API_KEY")
    if not key:
        sys.exit("AGENTMAIL_API_KEY is not set in the environment")
    from agentmail import AgentMail
    client = AgentMail(api_key=key)
    r = client.inboxes.messages.send(
        cfg["from_inbox"], to=to, subject=subject, html=html, text=text,
        attachments=[{"filename": fname, "content_type": "application/pdf",
                      "content": base64.b64encode(pdf.read_bytes()).decode()}],
        idempotency_key=re.sub(r"[^A-Za-z0-9._~-]", "-", f"mwb-{data['generated_local']}-{'-'.join(to)}"),
    )
    print(f"sent: message {r.message_id}")


if __name__ == "__main__":
    main()
