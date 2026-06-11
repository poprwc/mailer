"""
worker/worker.py — Background Worker (Render Worker Service).
Version : 5.0.0
- Límite diario GLOBAL (todos los emails del día, cualquier campaña)
- Solo procesa la campaña en status='sending'
- Si límite alcanzado: espera hasta medianoche UTC y retoma solo
"""
import sys, os, time, smtplib, ssl, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (get_conn, get_setting, get_sent_today,
                       get_active_campaign_id, init_db, ph, now_sql)

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [WORKER] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("worker")

HARD_BOUNCE_CODES = {550,551,552,553,554,521,525}
HARD_BOUNCE_MSGS  = ["user unknown","no such user","invalid address",
                     "does not exist","mailbox not found","address rejected",
                     "undeliverable","bad destination"]

def classify_bounce(err):
    try:
        if int(err[:3]) in HARD_BOUNCE_CODES: return "hard"
    except Exception: pass
    return "hard" if any(m in err.lower() for m in HARD_BOUNCE_MSGS) else "soft"

def seconds_until_midnight_utc():
    now = datetime.now(timezone.utc)
    secs = (23 - now.hour) * 3600 + (59 - now.minute) * 60 + (60 - now.second)
    return max(secs, 60)

def send_one():
    P = ph()

    # ── Una sola campaña activa a la vez ──────────────────
    cid = get_active_campaign_id()
    if not cid:
        return False, 30   # sleep 30s

    # ── Límite diario GLOBAL ──────────────────────────────
    sent_today  = get_sent_today()
    daily_limit = int(get_setting("daily_limit", "300"))
    if sent_today >= daily_limit:
        wait = seconds_until_midnight_utc()
        log.info(f"Daily limit reached ({sent_today}/{daily_limit}). "
                 f"Resuming in {wait//3600}h {(wait%3600)//60}m.")
        return False, min(wait, 3600)  # re-check cada hora como máximo

    # ── Siguiente email pendiente ─────────────────────────
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"""
            SELECT el.id, el.email FROM email_list el
            WHERE el.campaign_id={P} AND el.status='pending'
            AND el.email NOT IN (SELECT email FROM unsubscribes)
            ORDER BY el.id LIMIT 1
        """, (cid,))
        row = c.fetchone()

    if not row:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE campaigns SET status='done' WHERE id={P}", (cid,))
        log.info(f"Campaign {cid} completed ✓")
        return False, 30

    eid, email = row[0], row[1]

    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT subject, html_body, text_body FROM campaigns WHERE id={P}", (cid,))
        camp = c.fetchone()

    subject, html_body, text_body = camp[0], camp[1], camp[2]
    app_url   = get_setting("app_url", "").rstrip("/")
    from_addr = get_setting("smtp_from")
    from_name = get_setting("smtp_from_name")
    unsub_url = f"{app_url}/unsub/{cid}/{email}"
    track_url = f"{app_url}/track/open/{cid}/{email}"
    pixel     = f'<img src="{track_url}" width="1" height="1" style="display:none" alt="">'
    unsub_html= (f'<div style="text-align:center;padding:20px 0;font-size:11px;color:#999;">'
                 f'To unsubscribe <a href="{unsub_url}" style="color:#999;">click here</a>.</div>')

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = formataddr((from_name, from_addr))
    msg["To"]      = email
    msg["List-Unsubscribe"]      = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(text_body + f"\n\n---\nUnsubscribe: {unsub_url}", "plain", "utf-8"))
    msg.attach(MIMEText(html_body + unsub_html + pixel, "html", "utf-8"))

    interval = int(get_setting("interval_minutes", "5"))

    try:
        ctx = ssl.create_default_context()
        port = int(get_setting("smtp_port", "587"))
        if port == 465:
            srv = smtplib.SMTP_SSL(get_setting("smtp_host"), port, timeout=30, context=ctx)
        else:
            srv = smtplib.SMTP(get_setting("smtp_host"), port, timeout=30)
            srv.ehlo(); srv.starttls(context=ctx); srv.ehlo()
        srv.login(get_setting("smtp_user"), get_setting("smtp_pass"))
        srv.sendmail(from_addr, email, msg.as_string())
        srv.quit()

        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE email_list SET status='sent', sent_at={now_sql()} WHERE id={P}", (eid,))
            c.execute(f"INSERT INTO send_log (campaign_id,email,status) VALUES ({P},{P},'sent')", (cid, email))
        log.info(f"✓ [{sent_today+1}/{daily_limit}] → {email}")
        return True, interval * 60

    except smtplib.SMTPRecipientsRefused as e:
        err = str(e); bounce = classify_bounce(err)
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE email_list SET status='failed',error={P},bounce_type={P} WHERE id={P}",
                      (err[:200], bounce, eid))
            c.execute(f"INSERT INTO send_log (campaign_id,email,status,error) VALUES ({P},{P},{P},{P})",
                      (cid, email, f"bounce_{bounce}", err[:200]))
        log.warning(f"✗ Bounce {bounce}: {email}")
        return True, interval * 60

    except Exception as e:
        err = str(e)
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE email_list SET status='failed',error={P} WHERE id={P}", (err[:200], eid))
            c.execute(f"INSERT INTO send_log (campaign_id,email,status,error) VALUES ({P},{P},'failed',{P})",
                      (cid, email, err[:200]))
        log.error(f"✗ Error: {email} — {err[:100]}")
        return True, interval * 60

def main():
    log.info("Worker v5.0.0 started. Watching for active campaigns...")
    init_db()
    while True:
        try:
            sent, sleep_secs = send_one()
            time.sleep(sleep_secs)
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
