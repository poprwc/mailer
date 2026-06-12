"""
shared/db.py — Database layer.
Render: PostgreSQL (DATABASE_URL=postgres://...)
Local dev: SQLite
"""
import os, sqlite3
from contextlib import contextmanager

DATABASE_URL = os.getenv("DATABASE_URL", "")

def _is_pg():
    return bool(DATABASE_URL and DATABASE_URL.startswith("postgres"))

_BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQLITE_PATH = os.path.join(_BASE_DIR, "mailer.db")

@contextmanager
def get_conn():
    """Cursor estándar — rows como tuplas (compatible con c[0], c[1]... en templates)."""
    if _is_pg():
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL, sslmode="require")
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

@contextmanager
def get_conn_dict():
    """RealDictCursor — rows como dict (para APIs JSON)."""
    if _is_pg():
        import psycopg2, psycopg2.extras
        conn = psycopg2.connect(DATABASE_URL, sslmode="require",
                                cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def ph():
    return "%s" if _is_pg() else "?"

def now_sql():
    return "NOW()" if _is_pg() else "datetime('now')"

def init_db():
    P = ph(); pg = _is_pg()
    with get_conn() as conn:
        c = conn.cursor()
        if pg:
            stmts = [
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
                """CREATE TABLE IF NOT EXISTS campaigns (
                    id SERIAL PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
                    html_body TEXT NOT NULL DEFAULT '', text_body TEXT NOT NULL DEFAULT '',
                    body_visual TEXT,
                    created_at TIMESTAMP DEFAULT NOW(), status TEXT DEFAULT 'draft')""",
                """CREATE TABLE IF NOT EXISTS email_list (
                    id SERIAL PRIMARY KEY, campaign_id INTEGER,
                    email TEXT NOT NULL, status TEXT DEFAULT 'pending',
                    sent_at TIMESTAMP, error TEXT, bounce_type TEXT)""",
                """CREATE TABLE IF NOT EXISTS send_log (
                    id SERIAL PRIMARY KEY, campaign_id INTEGER, email TEXT,
                    status TEXT, timestamp TIMESTAMP DEFAULT NOW(), error TEXT)""",
                """CREATE TABLE IF NOT EXISTS opens (
                    id SERIAL PRIMARY KEY, campaign_id INTEGER, email TEXT,
                    opened_at TIMESTAMP DEFAULT NOW(), ip TEXT, user_agent TEXT)""",
                """CREATE TABLE IF NOT EXISTS unsubscribes (
                    id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                    campaign_id INTEGER, unsub_at TIMESTAMP DEFAULT NOW())""",
                """CREATE TABLE IF NOT EXISTS templates (
                    id SERIAL PRIMARY KEY, name TEXT NOT NULL,
                    blocks_json TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW())""",
                # migrations seguras — no fallan si ya existe
                "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS body_visual TEXT",
                "CREATE INDEX IF NOT EXISTS idx_el_camp ON email_list(campaign_id)",
                "CREATE INDEX IF NOT EXISTS idx_el_status ON email_list(campaign_id,status)",
                "CREATE INDEX IF NOT EXISTS idx_opens ON opens(campaign_id)",
            ]
            for s in stmts:
                try: c.execute(s)
                except Exception: pass
        else:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS campaigns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    subject TEXT NOT NULL, html_body TEXT NOT NULL DEFAULT '',
                    text_body TEXT NOT NULL DEFAULT '', body_visual TEXT,
                    created_at TEXT DEFAULT (datetime('now')), status TEXT DEFAULT 'draft');
                CREATE TABLE IF NOT EXISTS email_list (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER,
                    email TEXT NOT NULL, status TEXT DEFAULT 'pending',
                    sent_at TEXT, error TEXT, bounce_type TEXT);
                CREATE TABLE IF NOT EXISTS send_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER,
                    email TEXT, status TEXT, timestamp TEXT DEFAULT (datetime('now')), error TEXT);
                CREATE TABLE IF NOT EXISTS opens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER,
                    email TEXT, opened_at TEXT DEFAULT (datetime('now')), ip TEXT, user_agent TEXT);
                CREATE TABLE IF NOT EXISTS unsubscribes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL,
                    campaign_id INTEGER, unsub_at TEXT DEFAULT (datetime('now')));
                CREATE TABLE IF NOT EXISTS templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    blocks_json TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')));
                CREATE INDEX IF NOT EXISTS idx_el_camp ON email_list(campaign_id);
                CREATE INDEX IF NOT EXISTS idx_el_status ON email_list(campaign_id,status);
                CREATE INDEX IF NOT EXISTS idx_opens ON opens(campaign_id);
            """)
        defaults = {
            "smtp_host":"", "smtp_port":"587", "smtp_user":"", "smtp_pass":"",
            "smtp_from":"", "smtp_from_name":"", "daily_limit":"300",
            "interval_minutes":"5", "login_user":"admin", "login_pass":"admin123", "app_url":"",
            "imgbb_api_key":""
        }
        for k, v in defaults.items():
            try:
                if pg:
                    c.execute(f"INSERT INTO settings (key,value) VALUES ({P},{P}) ON CONFLICT (key) DO NOTHING",(k,v))
                else:
                    c.execute(f"INSERT OR IGNORE INTO settings (key,value) VALUES ({P},{P})",(k,v))
            except Exception:
                pass

def get_setting(key, default=""):
    with get_conn() as conn:
        c = conn.cursor()
        c.execute(f"SELECT value FROM settings WHERE key={ph()}", (key,))
        row = c.fetchone()
        return (row[0] if row else default) or default

def set_setting(key, value):
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        if _is_pg():
            c.execute(f"INSERT INTO settings (key,value) VALUES ({P},{P}) ON CONFLICT (key) DO UPDATE SET value={P}",(key,value,value))
        else:
            c.execute(f"INSERT OR REPLACE INTO settings (key,value) VALUES ({P},{P})",(key,value))

def get_campaign_stats(cid):
    P = ph()
    with get_conn() as conn:
        c = conn.cursor()
        def count(q,*a): c.execute(q,a); return c.fetchone()[0]
        total        = count(f"SELECT COUNT(*) FROM email_list WHERE campaign_id={P}",cid)
        pending      = count(f"SELECT COUNT(*) FROM email_list WHERE campaign_id={P} AND status='pending'",cid)
        sent         = count(f"SELECT COUNT(*) FROM email_list WHERE campaign_id={P} AND status='sent'",cid)
        failed       = count(f"SELECT COUNT(*) FROM email_list WHERE campaign_id={P} AND status='failed'",cid)
        bounced      = count(f"SELECT COUNT(*) FROM email_list WHERE campaign_id={P} AND bounce_type IS NOT NULL",cid)
        unique_opens = count(f"SELECT COUNT(DISTINCT email) FROM opens WHERE campaign_id={P}",cid)
        total_opens  = count(f"SELECT COUNT(*) FROM opens WHERE campaign_id={P}",cid)
        unsubs       = count(f"SELECT COUNT(*) FROM unsubscribes WHERE campaign_id={P}",cid)
    return {
        "total":total, "pending":pending, "sent":sent, "failed":failed, "bounced":bounced,
        "unique_opens":unique_opens, "total_opens":total_opens, "unsubs":unsubs,
        "open_rate":round(unique_opens/max(sent,1)*100,1),
        "unsub_rate":round(unsubs/max(sent,1)*100,1),
        "bounce_rate":round(bounced/max(sent,1)*100,1),
    }

def get_sent_today():
    """
    Límite diario GLOBAL — suma todos los emails enviados hoy
    sin importar de qué campaña vienen.
    """
    with get_conn() as conn:
        c = conn.cursor()
        if _is_pg():
            c.execute("SELECT COUNT(*) FROM send_log WHERE status='sent' AND timestamp::date=CURRENT_DATE")
        else:
            c.execute("SELECT COUNT(*) FROM send_log WHERE status='sent' AND date(timestamp)=date('now')")
        return c.fetchone()[0]

def get_active_campaign_id():
    """Devuelve el id de la campaña en status='sending', o None."""
    with get_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM campaigns WHERE status='sending' LIMIT 1")
        row = c.fetchone()
        return row[0] if row else None
