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

import os, re, sys, logging, functools, base64, uuid
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

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "static", "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
ALLOWED_IMG_EXT = {"png","jpg","jpeg","gif","webp"}
MAX_IMG_BYTES = 2 * 1024 * 1024  # 2 MB

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

# ─── Helpers ────────────────────────────────────────────────
def html_to_text(html):
    """Convierte HTML simple a texto plano para el cuerpo alternativo del email."""
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.S|re.I)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.S|re.I)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.I)
    text = re.sub(r'</(p|div|tr|h[1-6]|li)>', '\n', text, flags=re.I)
    text = re.sub(r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', r'\2 (\1)', text, flags=re.S|re.I)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    return text.strip()

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
        return redirect(url_for("visual_editor", campaign_id=cid))
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
    html_body = d.get("html_body","")
    text_body = d.get("text_body","").strip()
    if not text_body and html_body:
        text_body = html_to_text(html_body)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET name={P},subject={P},html_body={P},text_body={P} WHERE id={P}",
                  (d.get("name",""), d.get("subject",""),
                   html_body, text_body,
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
    text_body = html_to_text(html_body)
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE campaigns SET html_body={P}, body_visual={P}, text_body={P} WHERE id={P}",
                  (html_body, body_visual, text_body, campaign_id))
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

@app.route("/api/templates")
@login_required
def api_templates_list():
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute("SELECT id, name, created_at FROM templates ORDER BY created_at DESC")
        rows = c.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/templates/<int:tid>")
@login_required
def api_templates_get(tid):
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT name, blocks_json FROM templates WHERE id={P}", (tid,))
        row = c.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "Not found"}), 404
    return jsonify({"ok": True, "name": row[0], "blocks_json": row[1]})

@app.route("/api/templates", methods=["POST"])
@login_required
def api_templates_save():
    d = request.get_json() or {}
    name = (d.get("name") or "").strip()
    blocks_json = d.get("blocks_json", "")
    if not name or not blocks_json:
        return jsonify({"ok": False, "error": "Nombre y diseño son requeridos"}), 400
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"INSERT INTO templates (name, blocks_json) VALUES ({P},{P})", (name, blocks_json))
    return jsonify({"ok": True})

@app.route("/api/templates/<int:tid>", methods=["DELETE"])
@login_required
def api_templates_delete(tid):
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"DELETE FROM templates WHERE id={P}", (tid,))
    return jsonify({"ok": True})


@app.route("/api/upload-image", methods=["POST"])
@login_required
def api_upload_image():
    """
    Recibe { filename, data_url } (data_url = 'data:image/png;base64,....').

    - Si hay un ImgBB API key configurado (Settings → Image hosting),
      sube la imagen a ImgBB (gratis, persistente, sin login en el server).
    - Si no hay key, hace fallback a almacenamiento local en
      web/static/uploads/ (AVISO: en Render free el disco es efímero —
      se pierde en cada redeploy/restart).
    """
    d = request.get_json() or {}
    data_url = d.get("data_url", "")

    m = re.match(r"^data:image/(\w+);base64,(.+)$", data_url)
    if not m:
        return jsonify({"ok": False, "error": "Invalid image format"}), 400

    ext = m.group(1).lower()
    if ext == "jpeg": ext = "jpg"
    if ext not in ALLOWED_IMG_EXT:
        return jsonify({"ok": False, "error": f"Extension not allowed: {ext}"}), 400

    b64_data = m.group(2)
    try:
        raw = base64.b64decode(b64_data)
    except Exception:
        return jsonify({"ok": False, "error": "Could not decode image"}), 400

    if len(raw) > MAX_IMG_BYTES:
        return jsonify({"ok": False, "error": "Image too large (max 2MB)"}), 400

    imgbb_key = get_setting("imgbb_api_key", "").strip()

    if imgbb_key:
        try:
            import urllib.request, urllib.parse, json as _json
            payload = urllib.parse.urlencode({"key": imgbb_key, "image": b64_data}).encode()
            req = urllib.request.Request("https://api.imgbb.com/1/upload", data=payload, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = _json.loads(resp.read().decode())
            if result.get("success"):
                return jsonify({"ok": True, "url": result["data"]["url"], "host": "imgbb"})
            else:
                log.warning("ImgBB upload failed: %s — falling back to local storage", result)
        except Exception as e:
            log.warning("ImgBB upload error: %s — falling back to local storage", e)

    # ── Fallback: almacenamiento local (efímero en Render free) ──
    safe_name = f"{uuid.uuid4().hex}.{ext}"
    path = os.path.join(UPLOAD_DIR, safe_name)
    with open(path, "wb") as f:
        f.write(raw)

    app_url = get_setting("app_url", "").rstrip("/") or request.host_url.rstrip("/")
    url = f"{app_url}/static/uploads/{safe_name}"
    return jsonify({"ok": True, "url": url, "host": "local",
                    "warning": "Saved locally (ephemeral on Render free tier). "
                               "Set an ImgBB API key in Settings for persistent hosting."})


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

@app.route("/api/campaign/<int:cid>/bounces")
@login_required
def api_bounces(cid):
    """Agrupa los emails con bounce_type por tipo (hard/soft) y motivo."""
    P = ph()
    with get_conn_dict() as conn:
        c = conn.cursor()
        c.execute(f"""
            SELECT email, bounce_type, error, sent_at
            FROM email_list
            WHERE campaign_id={P} AND bounce_type IS NOT NULL
            ORDER BY bounce_type, email
        """, (cid,))
        rows = [dict(r) for r in c.fetchall()]

    hard = [r for r in rows if r["bounce_type"] == "hard"]
    soft = [r for r in rows if r["bounce_type"] == "soft"]
    return jsonify({"ok": True, "hard": hard, "soft": soft,
                    "total": len(rows), "hard_count": len(hard), "soft_count": len(soft)})

@app.route("/api/campaign/<int:cid>/bounces/remove", methods=["POST"])
@login_required
def api_bounces_remove(cid):
    """
    Elimina de la lista los emails con bounce. body: {"type": "hard"|"soft"|"all"}
    Hard = dirección inválida (recomendado eliminar siempre).
    Soft = falla temporal (eliminar opcional).
    """
    d = request.get_json() or {}
    btype = d.get("type", "all")
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        if btype == "all":
            c.execute(f"DELETE FROM email_list WHERE campaign_id={P} AND bounce_type IS NOT NULL", (cid,))
        else:
            c.execute(f"DELETE FROM email_list WHERE campaign_id={P} AND bounce_type={P}", (cid, btype))
        removed = c.rowcount
    return jsonify({"ok": True, "removed": removed})

@app.route("/api/campaign/<int:cid>/emails/import", methods=["POST"])@login_required
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
            "smtp_from_name","daily_limit","interval_minutes","login_user","login_pass","app_url","imgbb_api_key"]
    cfg = {k: get_setting(k) for k in keys}
    return render_template("settings.html", cfg=cfg)

@app.route("/api/settings/save", methods=["POST"])
@login_required
def api_settings_save():
    d = request.get_json() or {}
    allowed = {"smtp_host","smtp_port","smtp_user","smtp_pass","smtp_from",
               "smtp_from_name","daily_limit","interval_minutes","login_user","login_pass","app_url","imgbb_api_key"}
    for k, v in d.items():
        if k in allowed:
            set_setting(k, str(v))
    return jsonify({"ok": True})

@app.route("/api/settings/cron-url")
@login_required
def api_cron_url():
    app_url = get_setting("app_url","").rstrip("/")
    if not app_url:
        app_url = request.host_url.rstrip("/")
    token   = os.environ.get("CRON_TOKEN", "")
    if not token:
        return jsonify({
            "url": f"{app_url}/cron/send?token=MISSING_CRON_TOKEN",
            "warning": "CRON_TOKEN env var is not set on Render — set it manually in Environment settings."
        })
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
    token = os.environ.get("CRON_TOKEN", "")
    if not token or request.args.get("token") != token:
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
