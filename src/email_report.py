"""Run the SQL queries in ``src/sql/`` against Supabase and email an HTML report.

Every ``.sql`` file in ``src/sql/`` is a plain, read-only SELECT (one result set
per file). This runner executes each one against the Supabase Postgres, renders
the rows as an HTML table, stitches the tables into one email, and sends it via
Gmail SMTP. Add a new report section by dropping another ``.sql`` file in that
folder — no code change needed.

Secrets (GitHub Actions repo secrets, or ``.env`` locally)
----------------------------------------------------------
    SUPABASE_DB_URL     Postgres connection URI. Supabase dashboard → Connect →
                        "Session pooler" → URI. The session pooler (port 5432 on
                        the ...pooler.supabase.com host) is IPv4 so it works from
                        GitHub Actions, and keeps the session read-only flag this
                        script sets. Looks like:
                        postgresql://postgres.<ref>:<pw>@<host>:5432/postgres
                        (Avoid the IPv6-only "Direct connection".)
    GMAIL_USER          the Gmail address to send from
    GMAIL_APP_PASSWORD  a Google **App Password** (Google Account → Security →
                        2-Step Verification → App passwords). NOT your login
                        password.
    EMAIL_TO            recipient(s), comma-separated. Defaults to GMAIL_USER.

Run
---
    python3 -m pip install -r requirements-email.txt
    python src/email_report.py --client emokid690
    python src/email_report.py --client emokid690 --dry-run   # print, don't send
"""

from __future__ import annotations

import argparse
import html
import os
import smtplib
import ssl
import sys
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _util import load_env_file  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = Path(__file__).resolve().parent / "sql"
ROW_LIMIT = 25  # max rows rendered per table (report stays skimmable)


def _title_from_filename(path: Path) -> str:
    """joke_attribution.sql -> 'Joke Attribution'."""
    return path.stem.replace("_", " ").replace("-", " ").title()


def _fmt_cell(v) -> str:
    """Human-format one cell value for the HTML table."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        # whole-number floats -> int with separators; else trim trailing zeros
        return f"{int(v):,}" if v.is_integer() else f"{v:g}"
    if isinstance(v, (datetime, date)):
        return v.isoformat(sep=" ", timespec="minutes") if isinstance(v, datetime) else v.isoformat()
    return str(v)


def render_table(columns: list[str], rows: list[tuple], limit: int = ROW_LIMIT) -> str:
    """Render one result set as a self-contained (inline-styled) HTML table."""
    total = len(rows)
    shown = rows[:limit]
    th = "".join(
        f'<th style="text-align:left;padding:6px 10px;border-bottom:2px solid #ddd;'
        f'font:600 13px system-ui,sans-serif;color:#333;">{html.escape(str(c))}</th>'
        for c in columns
    )
    body_rows = []
    for i, row in enumerate(shown):
        bg = "#ffffff" if i % 2 == 0 else "#f7f7f8"
        tds = "".join(
            f'<td style="padding:6px 10px;border-bottom:1px solid #eee;'
            f'font:13px system-ui,sans-serif;color:#222;vertical-align:top;">'
            f'{html.escape(_fmt_cell(v))}</td>'
            for v in row
        )
        body_rows.append(f'<tr style="background:{bg};">{tds}</tr>')
    caption = f"{total} row{'s' if total != 1 else ''}"
    if total > limit:
        caption += f" (showing top {limit})"
    return (
        f'<p style="margin:4px 0 8px;font:12px system-ui,sans-serif;color:#888;">{caption}</p>'
        f'<div style="overflow-x:auto;"><table style="border-collapse:collapse;width:100%;'
        f'max-width:100%;">'
        f'<thead><tr>{th}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'
    )


def _run_query(cur, sql: str) -> tuple[list[str], list[tuple]]:
    cur.execute(sql)
    columns = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchall() if cur.description else []
    return columns, rows


def build_report(client: str) -> tuple[str, str, int]:
    """Run every query in SQL_DIR; return (subject, html_body, total_rows)."""
    try:
        import psycopg2
    except ImportError:
        raise SystemExit("psycopg2 not installed. Run: "
                         "python3 -m pip install -r requirements-email.txt")

    db_url = (os.getenv("SUPABASE_DB_URL") or "").strip().strip('"').strip("'")
    if not db_url:
        raise SystemExit(
            "Missing SUPABASE_DB_URL. Supabase dashboard → Connect → "
            "Session pooler → URI (port 5432)."
        )
    if not db_url.startswith(("postgresql://", "postgres://")):
        raise SystemExit(
            "SUPABASE_DB_URL must be the full connection URI starting with "
            "'postgresql://' — not just the password, and not the 'Connection "
            "parameters' block. Copy the Session pooler URI from Supabase → "
            "Connect, paste it as one line with no surrounding quotes:\n"
            "  postgresql://postgres.<ref>:<password>@<host>:5432/postgres"
        )

    sql_files = sorted(SQL_DIR.glob("*.sql"))
    if not sql_files:
        raise SystemExit(f"No .sql files found in {SQL_DIR}.")

    today = date.today().isoformat()
    sections: list[str] = []
    total_rows = 0

    conn = psycopg2.connect(db_url)
    try:
        conn.set_session(readonly=True, autocommit=True)  # reports never write
        with conn.cursor() as cur:
            for path in sql_files:
                sql = path.read_text(encoding="utf-8").strip().rstrip(";")
                title = _title_from_filename(path)
                try:
                    columns, rows = _run_query(cur, sql)
                    total_rows += len(rows)
                    table = render_table(columns, rows)
                except Exception as exc:  # one bad query shouldn't kill the report
                    table = (f'<p style="color:#b00;font:13px system-ui,sans-serif;">'
                             f'Query failed: {html.escape(str(exc))}</p>')
                sections.append(
                    f'<h2 style="margin:28px 0 6px;font:600 18px system-ui,sans-serif;'
                    f'color:#111;">{html.escape(title)}</h2>{table}'
                )
    finally:
        conn.close()

    body = (
        f'<div style="max-width:760px;margin:0 auto;padding:16px;">'
        f'<h1 style="font:700 22px system-ui,sans-serif;color:#111;margin:0 0 2px;">'
        f'{html.escape(client)} — weekly report</h1>'
        f'<p style="font:13px system-ui,sans-serif;color:#888;margin:0 0 8px;">{today}</p>'
        f'{"".join(sections)}'
        f'<p style="margin-top:32px;font:11px system-ui,sans-serif;color:#aaa;">'
        f'Generated from src/sql/ against Supabase.</p></div>'
    )
    subject = f"[{client}] weekly report — {today}"
    return subject, body, total_rows


def send_email(subject: str, html_body: str) -> None:
    user = (os.getenv("GMAIL_USER") or "").strip()
    # Google shows the 16-char App Password in 4 space-separated groups; SMTP wants
    # it with no spaces. A regular account password will be rejected by Gmail.
    password = (os.getenv("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not password:
        raise SystemExit(
            "Missing GMAIL_USER / GMAIL_APP_PASSWORD. Create a Google App Password "
            "(Google Account → Security → 2-Step Verification → App passwords) — "
            "your normal Gmail password will not work for SMTP."
        )
    recipients = [r.strip() for r in (os.getenv("EMAIL_TO") or user).split(",") if r.strip()]

    msg = MIMEText(html_body, "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)

    ctx = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls(context=ctx)
        server.login(user, password)
        server.sendmail(user, recipients, msg.as_string())
    print(f"Sent report to {', '.join(recipients)}.")


def main() -> None:
    load_env_file(REPO_ROOT / ".env")
    parser = argparse.ArgumentParser(description="Email the src/sql/ report.")
    parser.add_argument("--client", required=True, help="client id / handle")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and print the report instead of sending")
    args = parser.parse_args()

    subject, body, total_rows = build_report(args.client)
    if args.dry_run:
        print(f"Subject: {subject}\n({total_rows} total rows)\n")
        print(body)
        return
    send_email(subject, body)


if __name__ == "__main__":
    main()
