"""
worker/cron.py — Cron Job para Render (gratis).
Se ejecuta cada 5 minutos, envía UN email pendiente y termina.
Render Cron Jobs son gratuitos.
"""
import sys, os, smtplib, ssl, logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    get_conn, get_setting, get_sent_today, init_db, ph
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CRON] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("cron")

HARD_BOUNCE_CODES = {550,551,552,553,554,521,525}
HARD_BOUNCE_MSGS  = [
    "user unknown","no such user","invalid address",
    "does not exist","mailbox not found","address rejected",
    "undeliverable","bad destination"
]

def classify_bounce(err_str):
    err_lower = err_str.lower()
    try:
        if int(err_str[:3]) in HARD_BOUNCE_CODES:
            return "hard"
    except Exception:
        pass
    return "hard" if any(m in err_lower for m in HARD_BOUNCE_MSGS) else "soft"

def build_message(to_email, subject, html_body, text_body, campaign_id, app_url):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    from_addr = get_setting("smtp_from")
    from_name = get_setting("smtp_from_name")
    msg["From"] = formataddr((from_name, from_addr))
    msg["To"]   = to_email
    unsub_url = f"{app_url}/unsub/{campaign_id}/{to_email}"
    msg["List-Unsubscribe"]      = f"<{unsub_url}>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    track_url = f"{app_url}/track/open/{campaign_id}/{to_email}"
    pixel     = f'<img src="{track_url}" width="1" height="1" style="display:none" alt="">'
    unsub_link = (
        f'<div style="text-align:center;padding:20px 0;font-size:11px;color:#999;">'
        f'Si no querés recibir más emails, '
        f'<a href="{unsub_url}" style="color:#999;">hacé clic aquí para darte de baja</a>.</div>'
    )
    msg.attach(MIMEText(text_body + f"\n\n---\nPara darte de baja: {unsub_url}", "plain", "utf-8"))
    msg.attach(MIMEText(html_body + unsub_link + pixel, "html", "utf-8"))
    return msg

def run():
    init_db()

    # Buscar campaña activa
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM campaigns WHERE status='sending' LIMIT 1")
        row = c.fetchone()

    if not row:
        log.info("Sin campañas activas. Nada que enviar.")
        return

    cid = row[0]

    # Chequear límite diario
    sent_today  = get_sent_today(cid)
    daily_limit = int(get_setting("daily_limit", "300"))
    if sent_today >= daily_limit:
        log.info(f"Límite diario alcanzado ({sent_today}/{daily_limit}). Esperando mañana.")
        return

    # Siguiente email pendiente (skip unsubscribes)
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"""
            SELECT el.id, el.email
            FROM email_list el
            WHERE el.campaign_id={P}
              AND el.status='pending'
              AND el.email NOT IN (SELECT email FROM unsubscribes)
            ORDER BY el.id
            LIMIT 1
        """, (cid,))
        row = c.fetchone()

    if not row:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE campaigns SET status='done' WHERE id={P}", (cid,))
        log.info(f"Campaña {cid} completada ✓")
        return

    eid, email = row[0], row[1]

    # Datos de la campaña
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT subject, html_body, text_body FROM campaigns WHERE id={P}", (cid,))
        camp = c.fetchone()

    subject, html_body, text_body = camp[0], camp[1], camp[2]
    app_url   = get_setting("app_url", "").rstrip("/")
    smtp_host = get_setting("smtp_host")
    smtp_port = int(get_setting("smtp_port", "587"))
    smtp_user = get_setting("smtp_user")
    smtp_pass = get_setting("smtp_pass")

    is_pg = "postgres" in os.getenv("DATABASE_URL", "")

    try:
        msg = build_message(email, subject, html_body, text_body, cid, app_url)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as srv:
            srv.ehlo(); srv.starttls(context=ctx); srv.login(smtp_user, smtp_pass)
            srv.sendmail(get_setting("smtp_from"), email, msg.as_string())

        with get_conn() as conn:
            c = conn.cursor()
            c.execute(
                f"UPDATE email_list SET status='sent', sent_at=NOW() WHERE id={P}" if is_pg else
                f"UPDATE email_list SET status='sent', sent_at=datetime('now') WHERE id={P}",
                (eid,)
            )
            c.execute(
                f"INSERT INTO send_log (campaign_id,email,status) VALUES ({P},{P},{P})",
                (cid, email, "sent")
            )
        log.info(f"✓ [{sent_today+1}/{daily_limit}] → {email}")

    except smtplib.SMTPRecipientsRefused as e:
        err    = str(e)
        bounce = classify_bounce(err)
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE email_list SET status='failed',error={P},bounce_type={P} WHERE id={P}", (err[:200], bounce, eid))
            c.execute(f"INSERT INTO send_log (campaign_id,email,status,error) VALUES ({P},{P},{P},{P})", (cid, email, f"bounce_{bounce}", err[:200]))
        log.warning(f"✗ Bounce {bounce}: {email}")

    except Exception as e:
        err = str(e)
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"UPDATE email_list SET status='failed',error={P} WHERE id={P}", (err[:200], eid))
            c.execute(f"INSERT INTO send_log (campaign_id,email,status,error) VALUES ({P},{P},{P},{P})", (cid, email, "failed", err[:200]))
        log.error(f"✗ Error: {email} — {err[:100]}")

if __name__ == "__main__":
    run()
