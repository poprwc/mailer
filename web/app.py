"""
Campaign Mailer — Web App
Version : 5.0.0
Cambios  :
  - Una sola campaña activa a la vez (enforced en UI y API)
  - Límite diario GLOBAL compartido entre todas las campañas
  - Editor Visual integrado (/campaign/<id>/visual)
  - API /api/campaign/<id>/set-body  + /api/campaign/<id>/body
  - body_visual auto-migrado en init_db (shared/db.py)
  - get_conn() para templates (tuplas), get_conn_dict() para APIs
  - Rutas corregidas: schema real (html_body/text_body/email_list)
"""

VERSION = "5.0.0"

import os, re, sys, logging, functools
from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, abort, Response)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    get_conn, get_conn_dict, get_setting, set_setting,
    get_campaign_stats, get_sent_today, get_active_campaign_id,
    init_db, ph, now_sql
)

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [WEB] %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ─── App ──────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "changeme-in-prod")

# ─── Template globals ─────────────────────────────────────
@app.context_processor
def inject_globals():
    return {"get_setting": get_setting, "version": VERSION}

# ─── Auth ──────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated

# ─── Spam score ────────────────────────────────────────────
SPAM_TRIGGERS = [
    r'\bfree\b', r'\bwin\b', r'\bprize\b', r'\bcash\b', r'\bmoney\b',
    r'\bclick here\b', r'\bact now\b', r'\blimited time\b', r'\bguaranteed\b',
    r'\b100%\b', r'\bno cost\b', r'\bviagra\b', r'\bcialis\b',
    r'\bweight loss\b', r'\bmake money\b', r'\bwork from home\b',
]
def calc_spam_score(subject, html_body, text_body=""):
    text = (subject + " " + html_body + " " + text_body).lower()
    hits = [t for t in SPAM_TRIGGERS if re.search(t, text, re.I)]
    caps = len(re.findall(r'[A-Z]{4,}', subject))
    excl = subject.count('!')
    score = min(len(hits) * 1.5 + caps * 0.5 + excl * 0.5, 10)
    tips = []
    if hits:     tips.append(f"Spam words: {', '.join(hits[:5])}")
    if caps > 1: tips.append(f"{caps} ALL-CAPS words in subject")
    if excl > 1: tips.append(f"{excl} exclamation marks in subject")
    return {"score": round(score, 1), "max": 10, "tips": tips, "triggers": hits}

# ══════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if u == get_setting("login_user", "admin") and p == get_setting("login_pass", "admin123"):
            session["logged_in"] = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Invalid username or password")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ══════════════════════════════════════════════════════════
#  DASHBOARD
# ══════════════════════════════════════════════════════════
@app.route("/")
@login_required
def index():
    # c[0]=id c[1]=name c[2]=subject c[3]=status c[4]=created_at
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT c.id, c.name, c.subject, c.status, c.created_at,
              (SELECT COUNT(*) FROM email_list WHERE campaign_id=c.id) AS total,
              (SELECT COUNT(*) FROM email_list WHERE campaign_id=c.id AND status='sent') AS sent,
              (SELECT COUNT(*) FROM email_list WHERE campaign_id=c.id AND status='pending') AS pending
            FROM campaigns c ORDER BY c.created_at DESC
        """)
        campaigns = c.fetchall()
    active_id = get_active_campaign_id()
    sent_today = get_sent_today()
    daily_limit = int(get_setting("daily_limit", "300"))
    return render_template("index.html",
        campaigns=campaigns,
        active_id=active_id,
        sent_today=sent_today,
        daily_limit=daily_limit)

# ══════════════════════════════════════════════════════════
#  CAMPAIGN CRUD
# ══════════════════════════════════════════════════════════
@app.route("/campaign/new", methods=["GET", "POST"])
@login_required
def campaign_new():
    if request.method == "POST":
        name    = request.form.get("name", "").strip()
        subject = request.form.get("subject", "").strip()
        if not name:
            return render_template("editor.html", campaign=None, stats=None,
                                   active_id=get_active_campaign_id(), error="Name is required")
        P = ph()
        with get_conn() as conn:
            c = conn.cursor()
            if _is_pg_app():
                c.execute(f"INSERT INTO campaigns (name,subject,html_body,text_body) VALUES ({P},{P},{P},{P}) RETURNING id",
                          (name, subject, "", ""))
                cid = c.fetchone()[0]
            else:
                c.execute(f"INSERT INTO campaigns (name,subject,html_body,text_body) VALUES ({P},{P},{P},{P})",
                          (name, subject, "", ""))
                cid = c.lastrowid
        return redirect(url_for("campaign_edit", campaign_id=cid))
    return render_template("editor.html", campaign=None, stats=None,
                           active_id=get_active_campaign_id())

def _is_pg_app():
    url = os.environ.get("DATABASE_URL", "")
    return bool(url and url.startswith("postgres"))

@app.route("/campaign/<int:campaign_id>")
@login_required
def campaign_edit(campaign_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM campaigns WHERE id=%s" % ph(), (campaign_id,))
        campaign = c.fetchone()
    if not campaign:
        abort(404)
    stats = get_campaign_stats(campaign_id)
    active_id = get_active_campaign_id()
    sent_today = get_sent_today()
    daily_limit = int(get_setting("daily_limit", "300"))
    return render_template("editor.html",
        campaign=campaign, stats=stats,
        active_id=active_id,
        sent_today=sent_today,
        daily_limit=daily_limit)

@app.route("/campaign/<int:campaign_id>/save", methods=["POST"])
@login_required
def campaign_save(campaign_id):
    d = request.get_json() or {}
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET name={P},subject={P},html_body={P},text_body={P} WHERE id={P}",
                  (d.get("name",""), d.get("subject",""),
                   d.get("html_body",""), d.get("text_body",""),
                   campaign_id))
    return jsonify({"ok": True})

@app.route("/api/campaign/delete/<int:campaign_id>", methods=["POST"])
@login_required
def campaign_delete(campaign_id):
    # No borrar si está corriendo
    active_id = get_active_campaign_id()
    if active_id == campaign_id:
        return jsonify({"ok": False, "error": "Can't delete an active campaign. Pause it first."}), 400
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"DELETE FROM email_list WHERE campaign_id={P}", (campaign_id,))
        c.execute(f"DELETE FROM send_log WHERE campaign_id={P}", (campaign_id,))
        c.execute(f"DELETE FROM opens WHERE campaign_id={P}", (campaign_id,))
        c.execute(f"DELETE FROM campaigns WHERE id={P}", (campaign_id,))
    return jsonify({"ok": True})

# ══════════════════════════════════════════════════════════
#  VISUAL EDITOR
# ══════════════════════════════════════════════════════════
@app.route("/campaign/<int:campaign_id>/visual")
@login_required
def visual_editor(campaign_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT * FROM campaigns WHERE id={ph()}", (campaign_id,))
        campaign = c.fetchone()
    if not campaign:
        abort(404)
    return render_template("visual_editor.html", campaign=campaign)

@app.route("/api/campaign/<int:campaign_id>/set-body", methods=["POST"])
@login_required
def api_set_body(campaign_id):
    data = request.get_json()
    if not data:
        return jsonify({"ok": False, "error": "No data"}), 400
    html_body   = data.get("body_html", "").strip()
    body_visual = data.get("body_visual", "")
    if not html_body:
        return jsonify({"ok": False, "error": "Empty HTML"}), 400
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET html_body={P}, body_visual={P} WHERE id={P}",
                  (html_body, body_visual, campaign_id))
    log.info("Visual body saved — campaign %s", campaign_id)
    return jsonify({"ok": True})

@app.route("/api/campaign/<int:campaign_id>/body")
@login_required
def api_get_body(campaign_id):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT html_body, body_visual FROM campaigns WHERE id={ph()}", (campaign_id,))
        row = c.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "html": row[0] or "", "visual": row[1] or ""})

# ══════════════════════════════════════════════════════════
#  EMAIL LIST
# ══════════════════════════════════════════════════════════
@app.route("/api/campaign/<int:cid>/emails")
@login_required
def api_emails(cid):
    page  = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))
    off   = (page - 1) * limit
    P = ph()
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute(f"""
            SELECT email, status, sent_at, error
            FROM email_list WHERE campaign_id={P}
            ORDER BY id DESC LIMIT {P} OFFSET {P}
        """, (cid, limit, off))
        rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/campaign/<int:cid>/emails/import", methods=["POST"])
@login_required
def api_import_emails(cid):
    data = request.get_json() or {}
    raw  = data.get("emails", "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "Empty list"}), 400
    inserted = skipped = 0
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        for line in raw.splitlines():
            line = line.strip()
            if not line: continue
            email = line.split(",")[0].strip().lower()
            if not re.match(r'^[^@]+@[^@]+\.[^@]+$', email):
                skipped += 1; continue
            try:
                if _is_pg_app():
                    c.execute(f"INSERT INTO email_list (campaign_id,email,status) VALUES ({P},{P},'pending') ON CONFLICT DO NOTHING",
                              (cid, email))
                else:
                    c.execute(f"INSERT OR IGNORE INTO email_list (campaign_id,email,status) VALUES ({P},{P},'pending')",
                              (cid, email))
                inserted += 1
            except Exception:
                skipped += 1
    return jsonify({"ok": True, "inserted": inserted, "skipped": skipped})

@app.route("/api/campaign/<int:cid>/emails/delete-all", methods=["POST"])
@login_required
def api_delete_all_emails(cid):
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"DELETE FROM email_list WHERE campaign_id={P}", (cid,))
    return jsonify({"ok": True})

# ══════════════════════════════════════════════════════════
#  QUEUE CONTROL — una campaña activa a la vez
# ══════════════════════════════════════════════════════════
@app.route("/api/queue/start", methods=["POST"])
@login_required
def api_queue_start():
    cid = request.json.get("campaign_id")
    # Verificar que no hay otra campaña corriendo
    active_id = get_active_campaign_id()
    if active_id and active_id != cid:
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"SELECT name FROM campaigns WHERE id={ph()}", (active_id,))
            row = c.fetchone()
            active_name = row[0] if row else f"#{active_id}"
        return jsonify({"ok": False,
                        "error": f'Campaign "{active_name}" is already sending. Pause it first.'}), 400
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET status='sending' WHERE id={P}", (cid,))
    return jsonify({"ok": True})

@app.route("/api/queue/stop", methods=["POST"])
@login_required
def api_queue_stop():
    cid = request.json.get("campaign_id")
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET status='paused' WHERE id={P}", (cid,))
    return jsonify({"ok": True})

@app.route("/api/queue/status/<int:cid>")
@login_required
def api_queue_status(cid):
    stats = get_campaign_stats(cid)
    sent_today  = get_sent_today()
    daily_limit = int(get_setting("daily_limit", "300"))
    active_id   = get_active_campaign_id()
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT status FROM campaigns WHERE id={P}", (cid,))
        row = c.fetchone()
    # Si está sending pero el límite diario se alcanzó → mostrar daily_limit en UI
    campaign_status = row[0] if row else "unknown"
    if campaign_status == "sending" and sent_today >= daily_limit:
        campaign_status = "daily_limit"
    return jsonify({
        "campaign_status":  campaign_status,
        "sent_today":       sent_today,
        "daily_limit":      daily_limit,
        "interval_minutes": int(get_setting("interval_minutes", "5")),
        "active_id":        active_id,
        **stats
    })

# ══════════════════════════════════════════════════════════
#  STATS / LOGS
# ══════════════════════════════════════════════════════════
@app.route("/api/stats/<int:cid>")
@login_required
def api_stats(cid):
    return jsonify(get_campaign_stats(cid))

@app.route("/api/opens/<int:cid>")
@login_required
def api_opens(cid):
    P = ph()
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute(f"SELECT email, opened_at, ip FROM opens WHERE campaign_id={P} ORDER BY opened_at DESC LIMIT 100", (cid,))
        rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/unsubscribes")
@login_required
def api_unsubscribes():
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute("SELECT email, campaign_id, unsub_at FROM unsubscribes ORDER BY unsub_at DESC LIMIT 200")
        rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/logs/<int:cid>")
@login_required
def api_logs(cid):
    P = ph()
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute(f"SELECT email, status, timestamp, error FROM send_log WHERE campaign_id={P} ORDER BY id DESC LIMIT 150", (cid,))
        rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/spam-score", methods=["POST"])
@login_required
def api_spam_score():
    d = request.json or {}
    return jsonify(calc_spam_score(d.get("subject",""), d.get("html_body",""), d.get("text_body","")))

# ══════════════════════════════════════════════════════════
#  SETTINGS
# ══════════════════════════════════════════════════════════
@app.route("/settings")
@login_required
def settings_page():
    keys = ["smtp_host","smtp_port","smtp_user","smtp_pass","smtp_from",
            "smtp_from_name","daily_limit","interval_minutes","login_user","login_pass","app_url"]
    cfg = {k: get_setting(k) for k in keys}
    return render_template("settings.html", cfg=cfg)

@app.route("/api/settings/save", methods=["POST"])
@login_required
def api_settings_save():
    d = request.get_json() or {}
    allowed = {"smtp_host","smtp_port","smtp_user","smtp_pass","smtp_from",
               "smtp_from_name","daily_limit","interval_minutes","login_user","login_pass","app_url"}
    for k, v in d.items():
        if k in allowed:
            set_setting(k, str(v))
    return jsonify({"ok": True})

@app.route("/api/settings/cron-url")
@login_required
def api_cron_url():
    app_url = get_setting("app_url","").rstrip("/")
    token   = os.environ.get("CRON_TOKEN", "changeme")
    return jsonify({"url": f"{app_url}/cron/send?token={token}"})

@app.route("/api/settings/test-smtp", methods=["POST"])
@login_required
def api_test_smtp():
    import smtplib, ssl
    from email.mime.text import MIMEText
    try:
        msg = MIMEText("Test OK from Campaign Mailer")
        msg["Subject"] = "Test SMTP — Campaign Mailer"
        msg["From"]    = get_setting("smtp_from") or get_setting("smtp_user")
        msg["To"]      = get_setting("smtp_user")
        port = int(get_setting("smtp_port", "587"))
        ctx  = ssl.create_default_context()
        if port == 465:
            srv = smtplib.SMTP_SSL(get_setting("smtp_host"), port, timeout=10, context=ctx)
        else:
            srv = smtplib.SMTP(get_setting("smtp_host"), port, timeout=10)
            srv.ehlo(); srv.starttls(context=ctx); srv.ehlo()
        srv.login(get_setting("smtp_user"), get_setting("smtp_pass"))
        srv.sendmail(msg["From"], [msg["To"]], msg.as_string())
        srv.quit()
        return jsonify({"ok": True, "msg": "Test email sent successfully"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

# ══════════════════════════════════════════════════════════
#  CRON ENDPOINT (cron-job.org lo llama cada 5 min)
# ══════════════════════════════════════════════════════════
@app.route("/cron/send")
def cron_send():
    token = os.environ.get("CRON_TOKEN", "changeme")
    if request.args.get("token") != token:
        abort(403)
    try:
        from worker.cron import run
        run()
        return "ok", 200
    except Exception as e:
        log.error("Cron error: %s", e)
        return str(e), 500

# ══════════════════════════════════════════════════════════
#  TRACKING & UNSUBSCRIBE (públicos)
# ══════════════════════════════════════════════════════════
@app.route("/track/open/<int:cid>/<path:email>")
def track_open(cid, email):
    try:
        P = ph()
        with get_conn() as conn:
            c = conn.cursor()
            c.execute(f"INSERT INTO opens (campaign_id,email,ip) VALUES ({P},{P},{P})",
                      (cid, email, request.remote_addr))
    except Exception:
        pass
    gif = (b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00'
           b'\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x00\x00\x00\x00\x00'
           b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b')
    return Response(gif, mimetype="image/gif")

@app.route("/unsub/<int:cid>/<path:email>", methods=["GET","POST"])
def unsubscribe(cid, email):
    if request.method == "POST":
        P = ph()
        try:
            with get_conn() as conn:
                c = conn.cursor()
                if _is_pg_app():
                    c.execute(f"INSERT INTO unsubscribes (email,campaign_id) VALUES ({P},{P}) ON CONFLICT (email) DO NOTHING",
                              (email, cid))
                else:
                    c.execute(f"INSERT OR IGNORE INTO unsubscribes (email,campaign_id) VALUES ({P},{P})",
                              (email, cid))
                c.execute(f"UPDATE email_list SET status='unsub' WHERE campaign_id={P} AND email={P}",
                          (cid, email))
        except Exception as e:
            log.error("Unsub error: %s", e)
        return render_template("unsub.html", email=email, done=True)
    return render_template("unsub.html", email=email, done=False)

# ══════════════════════════════════════════════════════════
#  BOOT
# ══════════════════════════════════════════════════════════
with app.app_context():
    try:
        init_db()
        log.info("Campaign Mailer v%s ready", VERSION)
    except Exception as e:
        log.error("DB init error: %s", e)

if __name__ == "__main__":
    app.run(debug=True, port=5000)
