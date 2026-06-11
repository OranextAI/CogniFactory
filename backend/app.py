import sqlite3
import time
import traceback
from datetime import datetime, timezone
from threading import Thread
import random
import re
import os
import json

import psycopg2
import psycopg2.pool
from psycopg2.extras import RealDictCursor

import requests
from bs4 import BeautifulSoup
from flask import Flask, g, jsonify, request, Response, send_file, send_from_directory
from flask_cors import CORS  # MODIFICATION: Imported CORS

# Imports for Ollama and LangChain
from langchain.chains import RetrievalQA
from langchain.prompts import PromptTemplate
from langchain_chroma import Chroma
from langchain_ollama import OllamaLLM
from langchain_core.messages import HumanMessage, AIMessage

# --- BACKUP: Original Mistral API imports (commented out) ---
# from langchain_mistralai import ChatMistralAI
# MODIFICATION: The get_embedding_function can cause errors if the file doesn't exist.
# We will wrap it to handle this case.
try:
    from get_embedding_function import get_embedding_function
except ImportError:
    print("WARNING: get_embedding_function not found. RAG will be disabled.")
    get_embedding_function = None


FRONTEND_DIST = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend', 'dist'))
app = Flask(__name__, static_folder=FRONTEND_DIST, static_url_path='')

# MODIFICATION: Enable CORS to allow requests from your React frontend
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Add a before_request handler to log all incoming requests
@app.before_request
def log_request():
    print(f"[REQUEST] {request.method} {request.path} from {request.remote_addr}")

DATABASE = "iot_dashboard.db"
EASA_NEWS_RSS_URL = 'https://www.easa.europa.eu/en/newsroom-and-events/news/feed.xml'

# --- Postgres configuration ---
# REQUIRED at runtime — set in systemd unit (or .env) before starting the app.
# Dashboard reads device/device_attributes/attributes/events/alert_historic.
# Writes are limited to alert_historic + abnormal_behavior.
#
# Example systemd Environment= lines:
#   Environment=PG_HOST=db.example.internal
#   Environment=PG_USER=cogni_reader
#   Environment=PG_PASSWORD=...   # use a dedicated read-only role
#   Environment=PG_DATABASE=mydb
#   Environment=DASHBOARD_FACTORY_ID=13
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DATABASE = os.getenv("PG_DATABASE", "postgres")
DASHBOARD_FACTORY_ID = os.getenv("DASHBOARD_FACTORY_ID", "1")
DISABLE_SENSOR_SIMULATOR = os.getenv("DISABLE_SENSOR_SIMULATOR", "1") not in ("0", "false", "False")

if not PG_PASSWORD:
    print("⚠️  PG_PASSWORD env var is empty — Postgres pool will fail to initialize.")

try:
    pg_pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1, maxconn=10,
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD,
        dbname=PG_DATABASE, connect_timeout=10,
    )
    print(f"✅ Postgres pool ready ({PG_HOST}:{PG_PORT}/{PG_DATABASE}, factory={DASHBOARD_FACTORY_ID})")
except Exception as _e:
    pg_pool = None
    print(f"❌ Postgres pool failed: {_e}")


class _PgConn:
    """Context manager that lends a pooled connection and returns it on exit."""

    def __enter__(self):
        if pg_pool is None:
            raise RuntimeError("Postgres pool not initialized")
        self._conn = pg_pool.getconn()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            pg_pool.putconn(self._conn)


def pg_query(sql, params=None):
    """SELECT helper -> list[dict]."""
    with _PgConn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()


def pg_one(sql, params=None):
    """SELECT helper -> dict or None."""
    rows = pg_query(sql, params)
    return rows[0] if rows else None


def pg_execute(sql, params=None):
    """INSERT/UPDATE helper. Use sparingly — write scope is alert_historic + abnormal_behavior only."""
    with _PgConn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())


# ---------------------------------------------------------------------------
# Production agent: schema digest + NL->SQL + read-only execution
# ---------------------------------------------------------------------------
# Curated subset of oranextdb schema relevant to "production process" questions.
# Keep this list short so it fits in a small LLM context. Kept in plain text
# (table(col TYPE, ...) -- comment) on purpose: it's smaller than CREATE TABLE
# and gives the LLM exactly the names it needs to write valid SQL.
PRODUCTION_SCHEMA_DIGEST = """\
-- Production lines master + status
production_lines(id uuid PK, department_id uuid, team_id uuid, code text, name text, status text, capacity_workers int, active bool, hr_department_id uuid, hr_team_id uuid, created_at timestamptz, updated_at timestamptz)
  -- status one of: active, inactive, maintenance, stopped
production_departments(id uuid PK, code text, name text, active bool)
production_teams(id uuid PK, department_id uuid, code text, name text, active bool)
production_line_machine_members(id uuid PK, line_id uuid FK->production_lines.id, post_id uuid, sequence_no int, active bool, operation_id uuid)
production_line_status_logs(id uuid PK, line_id uuid FK->production_lines.id, shift_id uuid, order_id uuid, order_item_id uuid, status text, started_at timestamptz, ended_at timestamptz, note text)
  -- status one of: running, stopped, waiting_material, waiting_staff, changeover, maintenance

-- Orders + items (what is being produced)
orders(id uuid PK, order_no text, client_id uuid FK->clients.id, created_by_user_id uuid, due_date date, max_lead_time_days int, client_budget numeric, status text, created_at timestamptz, updated_at timestamptz)
  -- status typically: pending, in_progress, done, cancelled
order_items(id uuid PK, order_id uuid FK->orders.id, article_ref text, article_desc text, category text, quantity int, priority text, smv_min_per_piece numeric, cost_per_min numeric, unit_price numeric, line_total numeric GENERATED, total_minutes numeric GENERATED, labor_total numeric GENERATED, due_date date, produced_qty int DEFAULT 0, plan_status text, in_progress_at timestamptz, completed_at timestamptz, last_checkpoint_at timestamptz, color_name text)
order_item_line_assignments(id uuid PK, order_id uuid FK->orders.id, order_item_id uuid FK->order_items.id, line_id uuid FK->production_lines.id, shift_id uuid, assignment_date date, assigned_qty numeric, status text)
  -- status one of: planned, active, done, cancelled
order_item_operation_sequences(id uuid PK, order_item_id uuid FK->order_items.id, operation_id uuid, sequence_no int, required bool, minutes_per_piece numeric)
order_item_fournitures(id uuid PK, order_item_id uuid FK->order_items.id, fourniture_id uuid, qty_needed numeric, qty_available numeric, qty_reserved numeric, qty_consumed numeric, unit text)

-- Aggregated views (already provided by the DB; cheap to use)
order_costs VIEW(order_id uuid, labor_total numeric, sales_total numeric, minutes_total numeric)
order_detail VIEW(id uuid, order_no text, client_id uuid, due_date date, status text, labor_total numeric, sales_total numeric, minutes_total numeric)
order_item_remaining VIEW(order_item_id uuid, order_id uuid, category text, remaining_qty int, smv_min_per_piece numeric)

-- Tracking (QR-scanner driven progress)
production_checkpoints(id uuid PK, order_item_id uuid FK->order_items.id, at timestamptz, qty_done int, minutes_spent int, station text, note text, user_id uuid)
production_tracking_units(id uuid PK, order_item_id uuid FK->order_items.id, line_id uuid FK->production_lines.id, status text, last_seen_at timestamptz)
production_tracking_events(id uuid PK, unit_id uuid, scanner_id uuid, event_type text, at timestamptz, payload jsonb)
production_tracking_alerts(id uuid PK, alert_type text, severity text)
production_tracking_scanners(id uuid PK, name text, location text)

-- Planning
plan_settings(id smallint PK, readiness_weights jsonb, min_readiness_ok numeric, employees int, hours_per_day numeric, working_days_per_week int, cost_per_minute_target numeric, target_rendement numeric)
plan_batches(id uuid PK)
plan_weeks(id uuid PK, batch_id uuid FK->plan_batches.id, week_index int, start_date date, end_date date)
plan_entries(id uuid PK)
production_plan(id uuid PK, order_item_id uuid FK->order_items.id, week_start date, planned_qty int, subcontractor_id uuid, status text)
  -- status default 'planned'

-- People / workstations
workers(id int PK, name text)
workstation(id int PK)
employees(id uuid PK)
shopfloor_operations(id uuid PK, code text, name text)
machine_operation_capabilities(id uuid PK, post_id uuid, operation_id uuid)

-- Clients (orders link here)
clients(id uuid PK, name text, email citext, phone text, company text, billing_address text)
subcontractors(id uuid PK, name text, contact text, phone text, email citext, active bool)

-- Factories
factories(id int PK, name text, address text)
"""

# Hard list of SQL keywords we refuse. Statements that contain them anywhere
# (outside string literals — we strip those before scanning) are rejected.
_FORBIDDEN_SQL_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "truncate", "create",
    "grant", "revoke", "comment", "vacuum", "analyze", "copy", "do", "call",
    "merge", "lock", "reindex", "cluster", "refresh", "listen", "notify",
    "begin", "commit", "rollback", "savepoint", "set", "reset", "execute",
)

_SQL_BLOCK_RE = re.compile(r"```(?:sql|postgres|postgresql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_SQL_STRIP_QUOTES_RE = re.compile(r"'(?:''|[^'])*'", re.DOTALL)


def _extract_sql(llm_text):
    """Pull a SQL statement out of an LLM response. Prefers ```sql blocks; falls back to first SELECT."""
    m = _SQL_BLOCK_RE.search(llm_text or "")
    if m:
        return m.group(1).strip()
    idx = (llm_text or "").lower().find("select")
    if idx == -1:
        return ""
    raw = llm_text[idx:].split("```")[0]
    return raw.strip().rstrip(";").strip()


def _validate_sql_safe(sql):
    """Return (ok, reason). Only accept a single SELECT/WITH statement, read-only keywords."""
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return False, "empty SQL"
    head = s.split(None, 1)[0].lower()
    if head not in ("select", "with"):
        return False, f"only SELECT/WITH allowed, got '{head}'"
    # Disallow multi-statement: there must be no ';' except possibly trailing (already stripped).
    if ";" in s:
        return False, "multiple statements forbidden"
    # Scan with string literals stripped to avoid false positives like "INSERT FROM client into..."
    no_strings = _SQL_STRIP_QUOTES_RE.sub("''", s).lower()
    for kw in _FORBIDDEN_SQL_KEYWORDS:
        # word-boundary match
        if re.search(r"\b" + re.escape(kw) + r"\b", no_strings):
            return False, f"forbidden keyword: {kw}"
    return True, ""


def _ensure_limit(sql, default_limit=200):
    """Append LIMIT N if the statement doesn't already have one."""
    if re.search(r"\blimit\b\s+\d+", sql, flags=re.IGNORECASE):
        return sql
    return sql.rstrip().rstrip(";") + f" LIMIT {default_limit}"


def _ollama_complete(prompt, model=None, timeout=120, temperature=None, num_ctx=12288):
    """One-shot completion via Ollama's /api/generate (no streaming).
    num_ctx is critical — qwen2.5vl's default context is small and a big schema digest
    will crash the runner. 8192 is enough for ~6k chars of schema + question + headroom.
    """
    url = f"{OLLAMA_BASE_URL}/api/generate"
    options = {"num_ctx": int(num_ctx)}
    if temperature is not None:
        options["temperature"] = float(temperature)
    payload = {"model": model or OLLAMA_MODEL, "prompt": prompt, "stream": False, "options": options}
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("response", "")


_INTROSPECT_CACHE = {}  # { (host, port, db): (epoch_seconds, digest_text) }
_INTROSPECT_TTL_S = 300


def _introspect_schema(conn, host_key):
    """Build a compact schema digest by querying information_schema on the user's live DB.
    Cached per (host, port, dbname) for 5 minutes so repeat questions don't re-query.
    """
    now = time.time()
    cached = _INTROSPECT_CACHE.get(host_key)
    if cached and (now - cached[0]) < _INTROSPECT_TTL_S:
        return cached[1]

    with conn.cursor() as cur:
        # Tables + comments
        cur.execute("""
            SELECT n.nspname, c.relname, obj_description(c.oid, 'pg_class') AS table_comment
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v', 'm')
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
            ORDER BY n.nspname, c.relname;
        """)
        tables = cur.fetchall()

        # Columns per table
        cur.execute("""
            SELECT table_schema, table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name, ordinal_position;
        """)
        cols_by_table = {}
        for sch, tbl, col, dtype, null in cur.fetchall():
            cols_by_table.setdefault((sch, tbl), []).append((col, dtype))

        # Foreign keys (best-effort, short form)
        cur.execute("""
            SELECT tc.table_schema, tc.table_name, kcu.column_name,
                   ccu.table_name AS ref_table, ccu.column_name AS ref_col
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
             AND tc.table_schema = ccu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema NOT IN ('pg_catalog', 'information_schema');
        """)
        fks_by_table = {}
        for sch, tbl, col, rt, rc_ in cur.fetchall():
            fks_by_table.setdefault((sch, tbl), []).append(f"{col}->{rt}.{rc_}")

    lines = []
    for sch, tbl, comment in tables:
        cols = cols_by_table.get((sch, tbl), [])
        if not cols:
            continue
        prefix = f"{sch}.{tbl}" if sch != "public" else tbl
        header = f"\n=== TABLE: {prefix} ===" + (f"  -- {comment}" if comment else "")
        lines.append(header)
        lines.append("Columns:")
        for c, d in cols:
            lines.append(f"  - {c} ({d})")
        fks = fks_by_table.get((sch, tbl))
        if fks:
            lines.append("Foreign keys: " + ", ".join(fks))

    digest = "\n".join(lines)
    _INTROSPECT_CACHE[host_key] = (now, digest)
    return digest


def _open_user_pg(db_config, statement_timeout_ms=15000):
    """Open a SHORT-LIVED read-only connection to the user-supplied DB.
    Caller is responsible for closing. Never reuse this connection across requests.
    """
    cfg = db_config or {}
    required = ("host", "user", "password", "database")
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError(f"db_config missing required fields: {missing}")
    kwargs = {
        "host": cfg["host"],
        "port": int(cfg.get("port") or 5432),
        "user": cfg["user"],
        "password": cfg["password"],
        "dbname": cfg["database"],
        "connect_timeout": 8,
    }
    if cfg.get("sslmode"):
        kwargs["sslmode"] = cfg["sslmode"]
    conn = psycopg2.connect(**kwargs)
    conn.set_session(readonly=True, autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %s", (statement_timeout_ms,))
    return conn


# --- Ollama Configuration ---
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")

print(f"Initializing Ollama with model: {OLLAMA_MODEL} at {OLLAMA_BASE_URL}")

# --- BACKUP: Original Mistral API Configuration (commented out) ---
# MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")
# if not MISTRAL_API_KEY:
#     print("CRITICAL ERROR: MISTRAL_API_KEY environment variable not set.")

RAG_PROMPT_TEMPLATE = """
Vous êtes Aerolyze, un assistant expert en conformité aéronautique.
Répondez à la question en vous basant uniquement sur le contexte suivant. Soyez concis, utile et répondez toujours en français.
Si vous ne trouvez pas la réponse dans le contexte, dites simplement "Je n'ai pas trouvé l'information dans les documents fournis."
Contexte: {context}
---
Question: {question}
"""
qa_chain = None
llm = None

# --- BACKUP: Original Mistral LLM initialization (commented out) ---
# # Initialize LLM first
# if MISTRAL_API_KEY:
#     try:
#         llm = ChatMistralAI(model="mistral-large-2512", mistral_api_key=MISTRAL_API_KEY, temperature=0.7)
#         print("✅ Mistral LLM initialized successfully.")
#     except Exception as e:
#         print(f"❌ CRITICAL ERROR: Failed to initialize Mistral LLM. Error: {e}")
#         llm = None
# else:
#     llm = None

# Initialize LLM with Ollama
try:
    llm = OllamaLLM(
        model=OLLAMA_MODEL,
        base_url=OLLAMA_BASE_URL,
        temperature=0.7
    )
    print("✅ Ollama LLM initialized successfully.")
except Exception as e:
    print(f"❌ CRITICAL ERROR: Failed to initialize Ollama LLM. Error: {e}")
    llm = None

# Initialize RAG chain
if llm and get_embedding_function:
    try:
        # --- BACKUP: Original Mistral RAG message (commented out) ---
        # print("Initializing RAG chain with Mistral...")
        print("Initializing RAG chain with Ollama...")
        CHROMA_PATH = "chroma"
        embedding_function = get_embedding_function()
        db_chroma = Chroma(persist_directory=CHROMA_PATH, embedding_function=embedding_function)
        retriever = db_chroma.as_retriever()
        prompt = PromptTemplate(template=RAG_PROMPT_TEMPLATE, input_variables=["context", "question"])
        qa_chain = RetrievalQA.from_chain_type(
            llm=llm, chain_type="stuff", retriever=retriever,
            return_source_documents=True, chain_type_kwargs={"prompt": prompt},
        )
        # --- BACKUP: Original Mistral RAG success message (commented out) ---
        # print("✅ RAG chain with Mistral initialized successfully.")
        print("✅ RAG chain with Ollama initialized successfully.")
    except Exception as e:
        qa_chain = None
        print(f"❌ WARNING: Failed to initialize RAG chatbot. RAG will be disabled. Error: {e}")
else:
    print("INFO: RAG chain initialization skipped due to missing LLM or embedding function.")


# --- Database Setup ---
def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()


# Serve the built React app (frontend/dist) + SPA fallback for client-side routes.
# API/video routes are registered explicitly elsewhere and win over this catch-all
# because Flask prefers rules with static prefixes over bare <path:...> variables.
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve_react(path):
    if path.startswith('api/') or path.startswith('videos/'):
        # Should never reach here if the specific route matched; return 404 just in case.
        return jsonify({"error": "Not found"}), 404
    full_path = os.path.join(FRONTEND_DIST, path)
    if path and os.path.isfile(full_path):
        return send_from_directory(FRONTEND_DIST, path)
    return send_from_directory(FRONTEND_DIST, 'index.html')

@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok", "model": OLLAMA_MODEL})


# SPA fallback: Flask's static handler intercepts root-level paths and returns 404 before
# the catch-all serve_react route gets a chance. This errorhandler catches those 404s and
# returns React's index.html so the client-side router (react-router) can handle them.
@app.errorhandler(404)
def spa_fallback(e):
    p = request.path.lstrip('/')
    if p.startswith('api/') or p.startswith('videos/') or p.startswith('healthz'):
        return jsonify({"error": "Not found", "path": request.path}), 404
    # If the requested path matches a real asset on disk, let send_from_directory serve it
    # (this handles cases where the static handler missed but the file exists).
    full_path = os.path.join(FRONTEND_DIST, p)
    if p and os.path.isfile(full_path):
        return send_from_directory(FRONTEND_DIST, p)
    return send_from_directory(FRONTEND_DIST, 'index.html')


# --- Chatbot API Endpoint ---
# MODIFICATION: Changed this endpoint to /api/ask to be consistent.
# It now returns a single JSON response instead of streaming to match the frontend.
@app.route("/api/ask", methods=["POST"])
def ask():
    # --- BACKUP: Original Mistral error check (commented out) ---
    # if not MISTRAL_API_KEY or not llm:
    #     return jsonify({"response": "Erreur: La clé API MISTRAL ou le LLM n'est pas configuré."}), 500
    if not llm:
        return jsonify({"response": "Erreur: Le LLM Ollama n'est pas configuré."}), 500
    
    request_data = request.json
    contents = request_data.get("contents")
    use_rag = request_data.get("use_rag", False)

    if not contents:
        return jsonify({"response": "Erreur: Aucun contenu fourni."}), 400
    
    if use_rag:
        if not qa_chain:
            return jsonify({"response": "Erreur: Le mode RAG n'est pas initialisé sur le serveur."}), 500
        last_q = contents[-1]['parts'][0]['text']
        try:
            result = qa_chain.invoke({"query": last_q})
            return jsonify({"response": result.get('result', "Pas de réponse.")})
        except Exception as e:
            return jsonify({"response": f"Erreur RAG: {e}"}), 500

    chat_history = [HumanMessage(content=m["parts"][0]["text"]) if m.get("role") == "user" else AIMessage(content=m["parts"][0]["text"]) for m in contents]
    
    try:
        result = llm.invoke(chat_history)
        response_text = result.content if hasattr(result, 'content') else str(result)
        return jsonify({"response": response_text})
    except Exception as e:
        print(f"Ollama API Error: {e}")
        return jsonify({"response": f"Erreur lors de la communication avec Ollama: {e}"}), 500


# --- API Routes for IoT Dashboard (backed by Postgres oranextdb) ---
#
# Schema mapping (Postgres -> the JSON shape the React dashboard expects):
#   "sensor" = a (device, attribute) pair    -> sensor.id = device_attributes.id
#   sensor.name                              -> device.name + " (" + attributes.attribute + ")"
#   sensor.type                              -> attributes.attribute
#   sensor.status                            -> derived: 'Actif' if events in last 24h else 'Inactif'
#   sensor.lat / sensor.lon                  -> None (Postgres only stores device.location text)
#   sensor.battery_level / min/max_threshold -> None
#   sensor.last_value / last_update          -> latest row in events for (iddevice, idattribute)

_STATUS_ACTIVE_WINDOW_HOURS = int(os.getenv("DASHBOARD_ACTIVE_WINDOW_HOURS", "8760"))  # 365d default — sim data is months old


def _build_sensor_rows():
    """One trip to Postgres returns one row per (device, attribute) within the configured factory,
    annotated with the most-recent event for that pair via LATERAL.
    """
    sql = """
        SELECT
            da.id           AS id,
            d.id            AS device_id,
            a.id            AS attribute_id,
            d.name          AS device_name,
            a.attribute     AS attribute,
            a.unit          AS unit,
            d.location      AS location,
            d.factory_id    AS factory_id,
            lv.value        AS last_value_raw,
            lv.timestamp    AS last_update
        FROM device d
        JOIN device_attributes da ON da.iddevice = d.id
        JOIN attributes a         ON a.id = da.idattribute
        LEFT JOIN LATERAL (
            SELECT value, timestamp
            FROM events e
            WHERE e.iddevice = d.id
              AND e.idattribute = a.id
              AND e.timestamp > NOW() - INTERVAL '180 days'
            ORDER BY timestamp DESC
            LIMIT 1
        ) lv ON TRUE
        WHERE d.factory_id = %s
        ORDER BY d.id, a.id;
    """
    rows = pg_query(sql, (DASHBOARD_FACTORY_ID,))
    now = datetime.now(tz=timezone.utc)
    active_cutoff_seconds = _STATUS_ACTIVE_WINDOW_HOURS * 3600
    sensors = []
    for r in rows:
        last_update = r["last_update"]
        try:
            last_value = float(r["last_value_raw"]) if r["last_value_raw"] is not None else None
        except (TypeError, ValueError):
            last_value = None  # non-numeric (e.g. 'fire_detected'), keep raw via separate field
        if last_update is not None:
            age = (now - last_update).total_seconds()
            status = "Actif" if age <= active_cutoff_seconds else "Inactif"
        else:
            status = "Inactif"
        sensors.append({
            "id": r["id"],
            "device_id": r["device_id"],
            "attribute_id": r["attribute_id"],
            "name": f'{r["device_name"]} ({r["attribute"]})'.strip(),
            "type": (r["attribute"] or "").strip(),
            "unit": r["unit"],
            "location": r["location"],
            "factory_id": r["factory_id"],
            "status": status,
            "lat": None,
            "lon": None,
            "battery_level": None,
            "min_threshold": None,
            "max_threshold": None,
            "last_value": last_value,
            "last_value_raw": r["last_value_raw"],
            "last_update": last_update.isoformat() if last_update else None,
        })
    return sensors


# ---------------------------------------------------------------------------
# Production Agent endpoints
# ---------------------------------------------------------------------------
@app.route('/api/production/schema-preview', methods=['POST'])
def production_schema_preview():
    """Return the exact schema digest the LLM receives for /api/ask-production.
    Lets you verify that newly-added tables are visible. Optional ?include_columns=0
    to return only the table list instead of the full digest.
    """
    data = request.json or {}
    db_config = data.get("db_config") or {}
    try:
        conn = _open_user_pg(db_config)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Connexion échouée: {e}"}), 400
    try:
        host_key = (db_config.get("host"), int(db_config.get("port") or 5432), db_config.get("database"))
        # Force a refresh so the user sees current state, not a 5-min-cached copy.
        _INTROSPECT_CACHE.pop(host_key, None)
        digest = _introspect_schema(conn, host_key)
        # Extract table names for a quick scan.
        tables = re.findall(r"=== TABLE: (\S+) ===", digest)
        return jsonify({
            "table_count": len(tables),
            "tables": tables,
            "digest_chars": len(digest),
            "digest_chars_sent_to_llm": min(len(digest), 18000),
            "digest_truncated": len(digest) > 18000,
            "digest": digest,
        })
    finally:
        conn.close()


@app.route('/api/test-db-connection', methods=['POST'])
def test_db_connection():
    """Verify a Postgres db_config quickly. Returns the server version + table count on success."""
    data = request.json or {}
    db_config = data.get("db_config") or {}
    try:
        conn = _open_user_pg(db_config)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"Connexion échouée: {e}"}), 400
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, version()")
            db, user, ver = cur.fetchone()
            cur.execute("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema NOT IN ('pg_catalog','information_schema')
                  AND table_type='BASE TABLE'
            """)
            n_tables = cur.fetchone()[0]
        return jsonify({
            "ok": True,
            "database": db,
            "user": user,
            "version": ver.split(",")[0],
            "table_count": n_tables,
        })
    finally:
        conn.close()


@app.route('/api/ask-production', methods=['POST'])
def ask_production():
    """Specialized agent: turns a natural-language question about the production
    process into a safe, read-only Postgres SELECT, executes it, then asks the
    LLM to answer in plain language using the result rows.

    Request body:
      {
        "question": "Combien de lignes de production sont actives ?",
        "db_config": { "host": "...", "port": 5432, "user": "...",
                       "password": "...", "database": "oranextdb",
                       "sslmode": "prefer" }   # optional
      }
    Response:
      { "answer": "...", "sql": "...", "columns": [...], "rows": [...],
        "row_count": <int>, "model": "qwen2.5vl" }
    """
    if not llm:
        return jsonify({"error": "Le LLM Ollama n'est pas configuré."}), 500

    body = request.json or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Champ 'question' manquant."}), 400

    db_config = body.get("db_config") or {}

    # 1. Open user's DB (read-only) early to validate creds before burning an LLM call.
    try:
        conn = _open_user_pg(db_config)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Connexion à la base de données échouée: {e}"}), 400

    try:
        # 2. Introspect the LIVE schema so the LLM only knows about tables that actually exist.
        host_key = (db_config.get("host"), int(db_config.get("port") or 5432), db_config.get("database"))
        try:
            live_schema = _introspect_schema(conn, host_key)
        except Exception as e:
            live_schema = ""
            print(f"[ask-production] introspection failed: {e}")

        # If the live introspection turned up nothing, fall back to the curated digest as a hint.
        schema_for_prompt = live_schema or PRODUCTION_SCHEMA_DIGEST

        # 3. NL -> SQL via Ollama. Prompt is strict: SQL only, no explanation.
        sql_prompt = f"""Tu es un assistant SQL pour PostgreSQL. Tu reçois le schéma RÉEL d'une base et une question. Écris UNE SEULE requête SELECT (ou WITH ... SELECT) PostgreSQL valide.

RÈGLES ABSOLUES — INTERDICTION DE LES VIOLER:
1. Utilise EXCLUSIVEMENT les tables qui apparaissent dans "=== TABLE: ... ===" ci-dessous.
2. Utilise EXCLUSIVEMENT les colonnes qui apparaissent sous "Columns:" de la table choisie.
3. NE JAMAIS inventer une colonne. Si tu écris "WHERE x.status = ..." mais que la colonne "status" n'est pas listée pour la table x, c'est interdit.
4. Si la question ne peut pas être répondue avec ce schéma, renvoie un bloc ```sql``` vide.
5. Réponds UNIQUEMENT par un bloc ```sql ... ``` — pas d'explication, pas de commentaire.

# Schéma RÉEL de la base (chaque table avec ses colonnes exactes):
{schema_for_prompt[:18000]}

# Question:
{question}

# Réponse (un seul bloc ```sql ... ```):"""
        try:
            llm_sql_text = _ollama_complete(sql_prompt, timeout=120, temperature=0.0)
        except Exception as e:
            return jsonify({"error": f"Erreur LLM (génération SQL): {e}"}), 500

        sql = _extract_sql(llm_sql_text)
        if not sql:
            return jsonify({
                "error": "Le LLM n'a pas produit de SQL exploitable.",
                "llm_raw": llm_sql_text[:1000],
            }), 422

        # Try to execute; on PG error feed it back to the LLM once for self-repair.
        rows = []
        columns = []
        last_error = None
        attempted_sqls = []
        for attempt in range(2):
            ok, reason = _validate_sql_safe(sql)
            if not ok:
                return jsonify({
                    "error": f"SQL refusée par le filtre de sécurité ({reason}).",
                    "sql": sql,
                    "attempts": attempted_sqls,
                }), 400
            sql_with_limit = _ensure_limit(sql, default_limit=200)
            attempted_sqls.append(sql_with_limit)
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql_with_limit)
                    rows = cur.fetchall() if cur.description else []
                    columns = [d[0] for d in (cur.description or [])]
                sql = sql_with_limit  # final SQL we return
                last_error = None
                break
            except Exception as e:
                conn.rollback()
                last_error = str(e).strip()
                if attempt == 1:
                    break
                # Self-repair: ask LLM to fix it given the actual error
                repair_prompt = f"""Ta requête PostgreSQL a ÉCHOUÉ. Ne reproduis PAS la même requête.

ERREUR PostgreSQL:
{last_error}

ANALYSE: Si l'erreur dit 'column "X" does not exist', alors la colonne X N'EXISTE PAS — ne l'utilise PAS. Si 'relation "Y" does not exist', alors la table Y N'EXISTE PAS. Choisis une AUTRE colonne ou table qui figure dans le schéma. Si aucune n'existe, écris une requête qui retourne seulement ce qui EXISTE.

# Schéma RÉEL (utilise EXCLUSIVEMENT ces tables et colonnes):
{schema_for_prompt[:10000]}

# Question d'origine:
{question}

# Requête FAUTIVE à NE PAS reproduire:
{sql_with_limit}

# Nouvelle requête (différente, valide):"""
                try:
                    repair_text = _ollama_complete(repair_prompt, timeout=120, temperature=0.4)
                except Exception as ee:
                    last_error = f"{last_error} | LLM repair failed: {ee}"
                    break
                fixed = _extract_sql(repair_text)
                if not fixed:
                    break
                sql = fixed

        if last_error is not None:
            return jsonify({
                "error": f"Erreur SQL: {last_error}",
                "sql": attempted_sqls[-1] if attempted_sqls else sql,
                "attempts": attempted_sqls,
            }), 400

        # JSON-safe rows: stringify non-JSON types (datetime, Decimal, UUID).
        def _to_jsonable(v):
            if v is None or isinstance(v, (int, float, bool, str)):
                return v
            if isinstance(v, dict):
                return {k: _to_jsonable(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [_to_jsonable(x) for x in v]
            return str(v)
        rows_json = [{k: _to_jsonable(v) for k, v in r.items()} for r in rows]

        # 4. Build the natural-language answer with the row data.
        # Truncate to keep prompt small; LLM only needs a summary, not all rows.
        preview = rows_json[:40]
        answer_prompt = f"""Tu es un assistant production en français. Voici la question de l'utilisateur, la requête SQL exécutée, et les résultats. Rédige une réponse claire, factuelle, et concise (3-8 phrases) en français. Si les résultats sont vides, dis-le. Si tu ne peux pas répondre avec ces données, dis-le. Cite des chiffres précis si possible.

# Question utilisateur:
{question}

# SQL exécutée:
{sql}

# Résultats (top {len(preview)} sur {len(rows_json)} lignes):
{json.dumps(preview, ensure_ascii=False, default=str)[:6000]}

# Réponse en français:"""
        try:
            answer = _ollama_complete(answer_prompt, timeout=120).strip()
        except Exception as e:
            answer = f"(Erreur LLM lors de la synthèse: {e})"

        return jsonify({
            "answer": answer,
            "sql": sql,
            "columns": columns,
            "rows": rows_json,
            "row_count": len(rows_json),
            "model": OLLAMA_MODEL,
        })
    finally:
        conn.close()


@app.route('/api/sensors')
def get_sensors():
    try:
        return jsonify(_build_sensor_rows())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/stats')
def get_stats():
    try:
        sensors = _build_sensor_rows()
        total_sensors = len(sensors)
        active_sensors = sum(1 for s in sensors if s["status"] == "Actif")
        alerts = total_sensors - active_sensors  # 'Inactif' count

        by_type_counts = {}
        by_status_counts = {}
        for s in sensors:
            by_type_counts[s["type"] or "inconnu"] = by_type_counts.get(s["type"] or "inconnu", 0) + 1
            by_status_counts[s["status"]] = by_status_counts.get(s["status"], 0) + 1

        # Per-sensor trend: last 30 events.
        # Fetched in a single query so we don't make N round-trips for N sensors.
        device_attr_pairs = [(s["device_id"], s["attribute_id"]) for s in sensors]
        trends_by_key = {pair: [] for pair in device_attr_pairs}
        if device_attr_pairs:
            sql = """
                SELECT ranked.iddevice, ranked.idattribute, ranked.value, ranked.timestamp
                FROM (
                    SELECT e.iddevice, e.idattribute, e.value, e.timestamp,
                           ROW_NUMBER() OVER (PARTITION BY e.iddevice, e.idattribute
                                              ORDER BY e.timestamp DESC) AS rn
                    FROM events e
                    WHERE (e.iddevice, e.idattribute) IN %s
                      AND e.timestamp > NOW() - INTERVAL '180 days'
                ) ranked
                WHERE ranked.rn <= 30
                ORDER BY ranked.iddevice, ranked.idattribute, ranked.timestamp DESC;
            """
            rows = pg_query(sql, (tuple(device_attr_pairs),))
            for r in rows:
                key = (r["iddevice"], r["idattribute"])
                trends_by_key.setdefault(key, []).append(r)

        sensor_trends = []
        for s in sensors:
            key = (s["device_id"], s["attribute_id"])
            points = trends_by_key.get(key, [])
            if not points:
                continue
            formatted = []
            for p in reversed(points):  # chronological for the chart
                ts = p["timestamp"]
                try:
                    value = float(p["value"]) if p["value"] is not None else 0
                except (TypeError, ValueError):
                    value = 0
                formatted.append({
                    "time": ts.strftime("%H:%M") if ts else "",
                    "value": value,
                    "timestamp": ts.isoformat() if ts else None,
                })
            sensor_trends.append({
                "id": s["id"],
                "name": s["name"],
                "data": formatted,
                "latest_value": s["last_value"],
                "data_points_count": len(formatted),
            })

        return jsonify({
            "by_type": [{"name": k, "value": v} for k, v in by_type_counts.items()],
            "by_status": [{"name": k, "value": v} for k, v in by_status_counts.items()],
            "total_sensors": total_sensors,
            "active_sensors": active_sensors,
            "alerts": alerts,
            "sensor_trends": sensor_trends,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/sensors/lookup')
def sensors_lookup():
    """Translate any identifier (device_id, attribute_id, name, …) into the canonical sensor row.
    Useful for frontends that store their own IDs and need to find the matching backend sensor_id.

    Examples:
        GET /api/sensors/lookup?device_id=66&attribute_id=31
        GET /api/sensors/lookup?attribute_id=34
        GET /api/sensors/lookup?name=Ora_Carbon-Dioxide
    """
    data = request.args.to_dict()
    sensor = _resolve_sensor(data)
    if not sensor:
        return jsonify(_resolve_sensor_hint(data)), 404
    return jsonify({
        "sensor_id": sensor["id"],
        "device_id": sensor["iddevice"],
        "attribute_id": sensor["idattribute"],
        "device_name": sensor["device_name"],
        "attribute": sensor["type"],
        "unit": sensor["unit"],
        "location": sensor["location"],
        "factory_id": sensor["factory_id"],
    })


@app.route('/api/sensor-history/<int:sensor_id>')
def get_sensor_history(sensor_id):
    """Accepts either device_attributes.id (canonical) OR — via ?device_id=&attribute_id= query params — the pair."""
    try:
        # If query params present, prefer those (allows other frontends with only device_id+attribute_id)
        if request.args.get('device_id') and request.args.get('attribute_id'):
            meta = {
                "iddevice": int(request.args['device_id']),
                "idattribute": int(request.args['attribute_id']),
            }
        else:
            meta = pg_one(
                "SELECT iddevice, idattribute FROM device_attributes WHERE id = %s",
                (sensor_id,)
            )
        if not meta:
            return jsonify({"error": f"Sensor {sensor_id} not found"}), 404
        rows = pg_query(
            """
            SELECT value, timestamp
            FROM events
            WHERE iddevice = %s AND idattribute = %s
              AND timestamp > NOW() - INTERVAL '180 days'
            ORDER BY timestamp DESC
            LIMIT 50;
            """,
            (meta["iddevice"], meta["idattribute"]),
        )
        return jsonify([
            {"value": r["value"], "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None}
            for r in reversed(rows)
        ])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# Sensor CRUD endpoints disabled: the Postgres dashboard is read-only on device/attributes.
# Writes are permitted only against alert_historic / abnormal_behavior.
def _not_implemented(label):
    return jsonify({
        "error": (
            f"{label} is disabled — the dashboard now reads from Postgres oranextdb where "
            "device/attribute changes must happen upstream."
        )
    }), 501


@app.route('/api/add-sensor', methods=['POST'])
def add_sensor():
    return _not_implemented("add-sensor")


@app.route('/api/update-sensor/<int:sensor_id>', methods=['POST'])
def update_sensor(sensor_id):
    return _not_implemented(f"update-sensor/{sensor_id}")


@app.route('/api/delete-sensor/<int:sensor_id>', methods=['POST'])
def delete_sensor(sensor_id):
    return _not_implemented(f"delete-sensor/{sensor_id}")


@app.route('/api/activity-log')
def get_activity_log():
    """Recent operational events: pulled from alert_historic + abnormal_behavior."""
    try:
        rows = pg_query(
            """
            SELECT description, "date" FROM (
              SELECT alert AS description, "timestamp" AS "date"
                FROM alert_historic
              UNION ALL
              SELECT description AS description, "timestamp" AS "date"
                FROM abnormal_behavior
            ) merged
            ORDER BY "date" DESC NULLS LAST
            LIMIT 10;
            """
        )
        return jsonify([
            {"description": r["description"], "date": r["date"].isoformat() if r["date"] else None}
            for r in rows
        ])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/news')
def get_news():
    # No equivalent in Postgres; news lived in the SQLite changes_log table.
    return jsonify([])


@app.route('/api/unread-count')
def get_unread_count():
    return jsonify({"count": 0})

@app.route('/api/analyze-video-frame', methods=['POST'])
def analyze_video_frame():
    """Analyze a video frame using Mistral Vision Model"""
    if not MISTRAL_API_KEY:
        error_msg = "❌ MISTRAL_API_KEY environment variable not configured"
        print(f"[VIDEO_ANALYSIS] {error_msg}")
        return jsonify({"error": error_msg}), 500

    try:
        if 'frame' not in request.files:
            return jsonify({"error": "❌ Aucune image n'a été fournie."}), 400

        frame_file = request.files['frame']
        video_title = request.form.get('video_title', 'Vidéo inconnue')
        current_time = request.form.get('current_time', '0')

        if not frame_file or frame_file.filename == '':
            return jsonify({"error": "❌ Fichier vide."}), 400

        frame_data = frame_file.read()
        if not frame_data:
            return jsonify({"error": "❌ Fichier image vide."}), 400

        import base64
        frame_base64 = base64.b64encode(frame_data).decode('utf-8')

        print(f"[VIDEO_ANALYSIS] 📸 Frame captured: {len(frame_data)} bytes from {video_title} at {current_time}s")

        analysis_prompt = f"""
Vous êtes un expert en vision par ordinateur et en analyse d'images industrielles/de surveillance.
Analysez cette image provenant de: {video_title} (à {current_time}s)
"""

        try:
            print("[DEBUG] Importing mistralai...")
            import mistralai
            from mistralai import Mistral

            print("[DEBUG] mistralai version:", getattr(mistralai, "__version__", "UNKNOWN"))

            client = Mistral(api_key=MISTRAL_API_KEY)

            # 🔍 CRITICAL DEBUGS
            print("[DEBUG] Mistral client type:", type(client))
            print("[DEBUG] dir(client):")
            print(dir(client))

            print("[DEBUG] hasattr(client, 'chat'):", hasattr(client, "chat"))
            print("[DEBUG] hasattr(client, 'messages'):", hasattr(client, "messages"))

            if hasattr(client, "chat"):
                print("[DEBUG] dir(client.chat):")
                print(dir(client.chat))

            print("[VIDEO_ANALYSIS] 🚀 Calling Mistral API...")

            chat_response = client.chat.complete(
                model="mistral-small-latest",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": analysis_prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": f"data:image/jpeg;base64,{frame_base64}"
                            }
                        ]
                    }
                ]
            )

            print("[DEBUG] Raw response object:", chat_response)

            analysis_result = chat_response.choices[0].message.content

            print(f"[VIDEO_ANALYSIS] ✅ Frame analyzed successfully from {video_title}")
            return jsonify({"analysis": analysis_result})

        except Exception as mistral_error:
            print("[VIDEO_ANALYSIS] ❌ Mistral API Error CAUGHT")
            print("[DEBUG] Exception type:", type(mistral_error))
            print("[DEBUG] Exception repr:", repr(mistral_error))
            print("[DEBUG] Full traceback:")
            traceback.print_exc()
            return jsonify({"error": str(mistral_error)}), 500

    except Exception as e:
        print("[VIDEO_ANALYSIS] ❌ Server Error")
        print("[DEBUG] Exception type:", type(e))
        print("[DEBUG] Exception repr:", repr(e))
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/generate-summary', methods=['POST'])
def generate_summary():
    """Generate a summary report of all sensors using the configured LLM"""
    if not llm:
        return jsonify({"error": "Erreur: Le LLM n'est pas configuré."}), 500
    
    data = request.json
    sensors = data.get('sensors', [])
    
    if not sensors:
        return jsonify({"error": "Erreur: Aucun capteur fourni."}), 400
    
    try:
        sensor_analysis = []

        for sensor in sensors:
            sensor_id = sensor.get('id')

            # Last 10 events for this (device, attribute) sensor pair, looked up via device_attributes.id
            try:
                rows = pg_query(
                    """
                    SELECT e.value
                    FROM events e
                    JOIN device_attributes da ON da.iddevice = e.iddevice AND da.idattribute = e.idattribute
                    WHERE da.id = %s
                      AND e.timestamp > NOW() - INTERVAL '180 days'
                    ORDER BY e.timestamp DESC
                    LIMIT 10;
                    """,
                    (sensor_id,),
                )
            except Exception as _e:
                rows = []
            values = []
            for r in reversed(rows):
                try:
                    values.append(float(r["value"]))
                except (TypeError, ValueError):
                    pass
            
            sensor_info = {
                'name': sensor.get('name'),
                'type': sensor.get('type'),
                'status': sensor.get('status'),
                'last_value': sensor.get('last_value'),
                'battery_level': sensor.get('battery_level'),
                'min_threshold': sensor.get('min_threshold'),
                'max_threshold': sensor.get('max_threshold'),
                'recent_values': values,
                'avg_value': sum(values) / len(values) if values else None
            }
            sensor_analysis.append(sensor_info)
        
        # Get global stats
        total_sensors = len(sensors)
        active_sensors = sum(1 for s in sensors if s.get('status') == 'Actif')
        inactive_sensors = sum(1 for s in sensors if s.get('status') == 'Inactif')
        maintenance_sensors = sum(1 for s in sensors if s.get('status') == 'Maintenance')
        
        # Build summary prompt
        summary_prompt = f"""
Vous êtes FactoryGuard AI, un assistant expert en supervision de capteurs IoT. Fournissez un rapport complet et structuré de la situation actuelle basé sur les données suivantes:

STATISTIQUES GLOBALES:
- Total capteurs: {total_sensors}
- Capteurs actifs: {active_sensors}
- Capteurs inactifs: {inactive_sensors}
- Capteurs en maintenance: {maintenance_sensors}

DÉTAILS DES CAPTEURS:
"""
        
        for sensor in sensor_analysis:
            last_value_str = f"{sensor['last_value']:.2f}" if sensor['last_value'] is not None else 'N/A'
            avg_value_str = f"{sensor['avg_value']:.2f}" if sensor['avg_value'] is not None else 'N/A'
            summary_prompt += f"""
  • {sensor['name']} ({sensor['type']})
    Status: {sensor['status']}
    Batterie: {sensor['battery_level']}%
    Dernière valeur: {last_value_str} (seuils: {sensor['min_threshold']}-{sensor['max_threshold']})
    Valeur moyenne: {avg_value_str}
"""
        
        summary_prompt += """

RAPPORT DEMANDÉ:
Veuillez générer un rapport détaillé incluant:
1. État général du système de capteurs
2. Capteurs à surveiller (anomalies, valeurs hors seuil)
3. État des batteries (alertes si nécessaire)
4. Recommandations prioritaires
5. Tendances observées et prévisions (si données suffisantes)

Soyez concis, clair et professionnel.
"""

        # Call Mistral
        result = llm.invoke([HumanMessage(content=summary_prompt)])
        summary_text = result.content if hasattr(result, 'content') else str(result)
        return jsonify({"summary": summary_text})
    
    except Exception as e:
        print(f"Erreur lors de la génération du rapport: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Erreur lors de la génération du rapport: {str(e)}"}), 500

def _resolve_sensor(data):
    """Resolve a sensor row from any of the natural identifier forms the
    different frontends might send. Returns the same dict shape as
    SELECT da.id, d.name AS device_name, a.attribute AS type, a.unit,
           d.location, d.factory_id, da.iddevice, da.idattribute
    or None if nothing matches.

    Accepted in payload (first non-empty wins):
      - sensor_id        — device_attributes.id (canonical, what /api/sensors returns)
      - device_attributes_id  — same as sensor_id (alias)
      - device_id + attribute_id  — the natural pair, ambiguity-free
      - device_id + idattribute   — same with frontend snake_case
      - iddevice + idattribute    — Postgres column names verbatim
      - attribute_id alone — only resolves if exactly one device produces it
      - device_id  alone   — only resolves if device has exactly one attribute
      - name        — fuzzy match on device.name (ILIKE), unique match required
    """
    base = """
        SELECT da.id, d.name AS device_name, a.attribute AS type, a.unit,
               d.location, d.factory_id, da.iddevice, da.idattribute
        FROM device_attributes da
        JOIN device d     ON d.id = da.iddevice
        JOIN attributes a ON a.id = da.idattribute
    """
    g = lambda *k: next((data.get(x) for x in k if data.get(x) is not None), None)

    sensor_id  = g('sensor_id', 'device_attributes_id')
    device_id  = g('device_id', 'iddevice')
    attr_id    = g('attribute_id', 'idattribute', 'attributeId')
    name       = g('name', 'sensor_name')

    if sensor_id is not None:
        return pg_one(base + " WHERE da.id = %s", (sensor_id,))

    if device_id is not None and attr_id is not None:
        return pg_one(base + " WHERE da.iddevice = %s AND da.idattribute = %s", (device_id, attr_id))

    if device_id is not None:
        rows = pg_query(base + " WHERE da.iddevice = %s", (device_id,))
        return rows[0] if len(rows) == 1 else None

    if attr_id is not None:
        rows = pg_query(base + " WHERE da.idattribute = %s", (attr_id,))
        return rows[0] if len(rows) == 1 else None

    if name:
        rows = pg_query(base + " WHERE d.name ILIKE %s", (name.strip(),))
        return rows[0] if len(rows) == 1 else None

    return None


def _resolve_sensor_hint(data):
    """Build a helpful 404 body listing what we received vs. what we expected."""
    received = {k: data.get(k) for k in (
        'sensor_id', 'device_id', 'attribute_id', 'idattribute', 'iddevice',
        'attributeId', 'name', 'sensor_name', 'device_attributes_id'
    ) if data.get(k) is not None}
    return {
        "error": "Capteur non trouvé.",
        "received": received,
        "hint": (
            "Send any of: {sensor_id} (= device_attributes.id from GET /api/sensors), "
            "{device_id, attribute_id}, or {name} (unique device name). "
            "Note: idattribute alone resolves only if exactly ONE device produces that attribute."
        ),
    }


@app.route('/api/diagnose-sensor', methods=['POST'])
def diagnose_sensor():
    """Diagnose a sensor using the configured LLM. Accepts multiple ID shapes — see _resolve_sensor."""
    if not llm:
        return jsonify({"error": "Erreur: Le LLM n'est pas configuré."}), 500

    data = request.json or {}
    sensor = _resolve_sensor(data)
    if not sensor:
        return jsonify(_resolve_sensor_hint(data)), 404

    try:
        sensor = {
            **sensor,
            "name": f"{sensor['device_name']} ({sensor['type']})",
            "status": "Actif",
            "battery_level": None,
            "min_threshold": None,
            "max_threshold": None,
        }

        # Last 20 events for this (device, attribute) pair
        sensor_data = pg_query(
            """
            SELECT value, timestamp FROM events
            WHERE iddevice = %s AND idattribute = %s
              AND timestamp > NOW() - INTERVAL '180 days'
            ORDER BY timestamp DESC
            LIMIT 20;
            """,
            (sensor["iddevice"], sensor["idattribute"]),
        )

        if not sensor_data:
            return jsonify({"error": "Aucune donnée disponible pour ce capteur."}), 400

        values = []
        for r in reversed(sensor_data):
            try:
                values.append(float(r["value"]))
            except (TypeError, ValueError):
                pass

        if not values:
            return jsonify({"error": "Les données récentes ne sont pas numériques."}), 400

        avg_value = sum(values) / len(values)
        min_value = min(values)
        max_value = max(values)

        diagnosis_prompt = f"""
Vous êtes un expert en diagnostic de capteurs IoT. Analysez le capteur suivant et fournissez un diagnostic détaillé:

Nom du capteur: {sensor['name']}
Type: {sensor['type']}
Statut: {sensor['status']}
Batterie: {sensor['battery_level']}%
Seuil Min: {sensor['min_threshold']}
Seuil Max: {sensor['max_threshold']}

Données récentes (20 dernières mesures):
Valeur moyenne: {avg_value:.2f}
Valeur min: {min_value:.2f}
Valeur max: {max_value:.2f}
Nombre de mesures: {len(values)}

Valeurs: {[f"{v:.2f}" for v in values]}

Veuillez fournir:
1. Un diagnostic de l'état du capteur
2. Les anomalies détectées (le cas échéant)
3. Les recommandations d'action
4. L'état de la batterie (critique, faible, normal)
"""

        result = llm.invoke([HumanMessage(content=diagnosis_prompt)])
        diagnosis_text = result.content if hasattr(result, 'content') else str(result)
        return jsonify({"diagnosis": diagnosis_text})

    except Exception as e:
        print(f"Erreur lors du diagnostic: {e}")
        traceback.print_exc()
        return jsonify({"error": f"Erreur lors du diagnostic: {str(e)}"}), 500

@app.route('/api/mark-read', methods=['POST'])
def mark_read():
    # changes_log table doesn't exist in Postgres oranextdb. Accept silently so the UI doesn't error.
    return jsonify({"status": "noop"})

@app.route('/api/videos', methods=['GET'])
def get_videos():
    """
    Récupère la liste de toutes les vidéos disponibles dans le dossier public/videos
    """
    try:
        videos_dir = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'public', 'videos')
        
        # Log pour débogage
        print(f"[DEBUG] Recherche des vidéos dans: {videos_dir}")
        print(f"[DEBUG] Le dossier existe: {os.path.exists(videos_dir)}")
        
        videos_list = []
        
        if os.path.exists(videos_dir):
            files = os.listdir(videos_dir)
            print(f"[DEBUG] Fichiers trouvés: {files}")
            
            for filename in files:
                if filename.lower().endswith(('.mp4', '.webm', '.ogg', '.mov')):
                    filepath = os.path.join(videos_dir, filename)
                    filesize = os.path.getsize(filepath)
                    print(f"[DEBUG] Ajout de la vidéo: {filename} ({filesize} bytes)")
                    videos_list.append({
                        'name': filename,
                        'path': f'/videos/{filename}',
                        'size': filesize,
                        'type': os.path.splitext(filename)[1].lower()
                    })
        else:
            print(f"[DEBUG] Le dossier n'existe pas!")
        
        # Trier par nom
        videos_list.sort(key=lambda x: x['name'])
        
        print(f"[DEBUG] Total vidéos trouvées: {len(videos_list)}")
        
        return jsonify({
            'videos': videos_list,
            'count': len(videos_list)
        })
    except Exception as e:
        print(f"Error fetching videos: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/videos/<path:filename>', methods=['GET', 'OPTIONS'])
def serve_video(filename):
    """
    Serve video files from the frontend/public/videos directory with proper CORS headers
    Handles both exact and case-insensitive file matching
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS, HEAD'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Range'
        response.headers['Access-Control-Max-Age'] = '3600'
        return response
    
    try:
        videos_dir = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'public', 'videos')
        videos_dir = os.path.abspath(videos_dir)
        
        print(f"\n[VIDEO_SERVE] ▶️ Request for video: {filename}")
        print(f"[VIDEO_SERVE] Base directory: {videos_dir}")
        
        # Try exact match first
        filepath = os.path.join(videos_dir, filename)
        filepath = os.path.abspath(filepath)
        
        # If exact match fails, try case-insensitive
        if not os.path.exists(filepath) and os.path.isdir(videos_dir):
            print(f"[VIDEO_SERVE] Exact match not found, trying case-insensitive...")
            try:
                files = os.listdir(videos_dir)
                print(f"[VIDEO_SERVE] Available files: {files}")
                
                for f in files:
                    if f.lower() == filename.lower():
                        filepath = os.path.join(videos_dir, f)
                        filepath = os.path.abspath(filepath)
                        print(f"[VIDEO_SERVE] ✅ Found case-insensitive match: {f}")
                        break
            except Exception as e:
                print(f"[VIDEO_SERVE] Error listing directory: {e}")
        
        # Security check
        if not filepath.startswith(videos_dir):
            print(f"[VIDEO_SERVE] ❌ Security blocked - path outside videos dir")
            return jsonify({'error': 'Access denied'}), 403
        
        # Check if file exists
        if not os.path.exists(filepath):
            print(f"[VIDEO_SERVE] ❌ File not found: {filepath}")
            return jsonify({'error': 'Video not found', 'requested': filename}), 404
        
        # Determine MIME type
        ext = os.path.splitext(filepath)[1].lower()
        mime_types = {
            '.mp4': 'video/mp4',
            '.webm': 'video/webm',
            '.ogg': 'video/ogg',
            '.mov': 'video/quicktime'
        }
        mime_type = mime_types.get(ext, 'application/octet-stream')
        
        file_size = os.path.getsize(filepath)
        print(f"[VIDEO_SERVE] ✅ Serving: {os.path.basename(filepath)}")
        print(f"[VIDEO_SERVE] Size: {file_size} bytes, MIME: {mime_type}")
        
        # Use send_file for proper streaming
        response = send_file(
            filepath,
            mimetype=mime_type,
            as_attachment=False
        )
        
        # Add CORS and caching headers
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Length, Content-Range'
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Cache-Control'] = 'public, max-age=86400'
        response.headers['Content-Type'] = mime_type
        
        return response
        
    except Exception as e:
        print(f"[VIDEO_SERVE] ❌ Exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'type': type(e).__name__}), 500


# --- Video Screenshot Analysis Endpoint ---
# Uses Ollama with Vision-Language Model (qwen2.5vl) to analyze video frames
@app.route('/api/analyze-video-screenshot', methods=['POST'])
def analyze_video_screenshot():
    """
    Analyze a screenshot from a video using Ollama's Vision-Language Model.
    
    Request:
        - video_file: The video file (optional, can use video_path instead)
        - video_path: Path to video file on server (optional)
        - timestamp: Time in seconds to extract frame from
        - question: Optional question about the frame
    
    Returns:
        - analysis: The VLM's analysis of the frame
    """
    print("[VIDEO_SCREENSHOT] 📸 Received screenshot analysis request")
    
    # Check if Ollama VLM is available
    if not llm:
        error_msg = "Erreur: Ollama LLM n'est pas configuré. Veuillez configurer OLLAMA_MODEL avec un modèle de vision."
        print(f"[VIDEO_SCREENSHOT] ❌ {error_msg}")
        return jsonify({"error": error_msg}), 500
    
    try:
        # Get video source - either uploaded file or server path
        video_path = request.form.get('video_path', '')
        timestamp = float(request.form.get('timestamp', 0))
        question = request.form.get('question', 'Décrivez ce que vous voyez dans cette image en détail.')
        
        frame_data = None
        
        # Option 1: Uploaded video file
        if 'video_file' in request.files and request.files['video_file'].filename:
            video_file = request.files['video_file']
            print(f"[VIDEO_SCREENSHOT] 📹 Processing uploaded video: {video_file.filename}")
            
            # Save temp video file
            import tempfile
            import subprocess
            import shutil
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as tmp_video:
                video_file.save(tmp_video.name)
                tmp_video_path = tmp_video.name
            
            try:
                # Extract frame at timestamp using ffmpeg
                with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_frame:
                    tmp_frame_path = tmp_frame.name
                
                # Run ffmpeg to extract frame
                result = subprocess.run([
                    'ffmpeg', '-y', '-ss', str(timestamp), 
                    '-i', tmp_video_path,
                    '-vframes', '1', '-q:v', '2',
                    tmp_frame_path
                ], capture_output=True, text=True)
                
                if result.returncode != 0:
                    print(f"[VIDEO_SCREENSHOT] ⚠️ ffmpeg error: {result.stderr}")
                    # Try alternative method
                    result = subprocess.run([
                        'ffmpeg', '-y', '-ss', str(timestamp),
                        '-i', tmp_video_path,
                        '-frames:v', '1', tmp_frame_path
                    ], capture_output=True, text=True)
                
                with open(tmp_frame_path, 'rb') as f:
                    frame_data = f.read()
                
                os.unlink(tmp_frame_path)
                
            finally:
                os.unlink(tmp_video_path)
        
        # Option 2: Server-side video path
        elif video_path:
            print(f"[VIDEO_SCREENSHOT] 📹 Processing server video: {video_path}")
            
            # Resolve full path
            if video_path.startswith('/videos/'):
                video_path = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'public', video_path.lstrip('/'))
            
            if not os.path.exists(video_path):
                return jsonify({"error": f"Vidéo non trouvée: {video_path}"}), 404
            
            # Extract frame using ffmpeg
            import tempfile
            import subprocess
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp_frame:
                tmp_frame_path = tmp_frame.name
            
            try:
                result = subprocess.run([
                    'ffmpeg', '-y', '-ss', str(timestamp),
                    '-i', video_path,
                    '-vframes', '1', '-q:v', '2',
                    tmp_frame_path
                ], capture_output=True, text=True)
                
                if result.returncode != 0:
                    print(f"[VIDEO_SCREENSHOT] ⚠️ ffmpeg error: {result.stderr}")
                    return jsonify({"error": "Erreur lors de l'extraction de l'image. Vérifiez que ffmpeg est installé."}), 500
                
                with open(tmp_frame_path, 'rb') as f:
                    frame_data = f.read()
            finally:
                os.unlink(tmp_frame_path)
        
        # Option 3: Direct image upload
        elif 'frame_image' in request.files and request.files['frame_image'].filename:
            frame_file = request.files['frame_image']
            frame_data = frame_file.read()
            print(f"[VIDEO_SCREENSHOT] 📷 Processing direct image upload: {frame_file.filename}")
        
        else:
            return jsonify({"error": "Aucune vidéo ou image fournie. Utilisez 'video_file', 'video_path', ou 'frame_image'."}), 400
        
        if not frame_data:
            return jsonify({"error": "Impossible d'extraire l'image de la vidéo."}), 500
        
        print(f"[VIDEO_SCREENSHOT] ✅ Frame extracted: {len(frame_data)} bytes")
        
        # Convert to base64 for Ollama
        import base64
        frame_base64 = base64.b64encode(frame_data).decode('utf-8')
        
        # Prepare prompt for VLM
        analysis_prompt = f"""Vous êtes un expert en analyse d'images industrielles et de vidéosurveillance.
Analysez cette image et répondez à la question suivante:

Question: {question}

Fournissez une description détaillée de ce que vous voyez, en particulier:
- Objets présents dans l'image
- Conditions d'éclairage
- Événements ou activités inhabituels
- Problèmes potentiels de sécurité
- État des équipements ou installations

Répondez en français de manière concise mais complète."""
        
        # Send to Ollama VLM
        print(f"[VIDEO_SCREENSHOT] 🤖 Sending to Ollama VLM ({OLLAMA_MODEL})...")
        
        try:
            # Use the vision-capable model with image input
            # For Ollama, we use the /api/generate endpoint with multimodal
            import requests as req
            
            ollama_url = f"{OLLAMA_BASE_URL}/api/generate"
            
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": analysis_prompt,
                "images": [frame_base64],
                "stream": False
            }
            
            response = req.post(ollama_url, json=payload, timeout=120)
            
            if response.status_code != 200:
                print(f"[VIDEO_SCREENSHOT] ❌ Ollama error: {response.status_code} - {response.text}")
                return jsonify({"error": f"Erreur Ollama: {response.text}"}), 500
            
            result = response.json()
            analysis = result.get('response', 'Pas de réponse du modèle.')
            
            print(f"[VIDEO_SCREENSHOT] ✅ Analysis complete: {len(analysis)} chars")
            
            return jsonify({
                "analysis": analysis,
                "timestamp": timestamp,
                "model": OLLAMA_MODEL
            })
            
        except Exception as ollama_error:
            print(f"[VIDEO_SCREENSHOT] ❌ Ollama API Error: {ollama_error}")
            traceback.print_exc()
            return jsonify({"error": f"Erreur lors de l'analyse: {str(ollama_error)}"}), 500
    
    except Exception as e:
        print(f"[VIDEO_SCREENSHOT] ❌ Server Error: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# Mettez cette fonction à la place de l'ancienne fonction vide ou de la simulation de base
def simulate_sensor_data_logging():
    """
    Cette fonction s'exécute dans un thread séparé en arrière-plan.
    Elle simule l'arrivée de nouvelles données de capteurs toutes les 5 secondes.
    """
    print("✅ [Simulation] Démarrage du thread de simulation des capteurs...")
    
    # Define realistic ranges and variations for each sensor type
    SENSOR_CONFIGS = {
        'Température': {
            'min_safe': 20,     # Normal room temperature range
            'max_safe': 23,
            'variation': 0.1    # Small temperature changes
        },
        'Humidité': {
            'min_safe': 40,     # Comfortable humidity range
            'max_safe': 50,
            'variation': 0.2    # Gradual humidity changes
        },
        'Pression': {
            'min_safe': 1013,   # Normal atmospheric pressure
            'max_safe': 1015,
            'variation': 0.1    # Minimal pressure changes
        },
        'Qualité de l\'air': {
            'min_safe': 30,     # Good AQI range
            'max_safe': 50,
            'variation': 0.3    # Gradual air quality changes
        }
    }

    def get_initial_value(sensor_type, min_threshold, max_threshold):
        config = SENSOR_CONFIGS.get(sensor_type)
        if not config:
            return (min_threshold + max_threshold) / 2
            
        # Start with a value in the middle of the safe range
        base_value = (config['min_safe'] + config['max_safe']) / 2
        # Add small random variation
        variation = (config['max_safe'] - config['min_safe']) * 0.1
        return base_value + random.uniform(-variation, variation)

    while True:
        try:
            conn = sqlite3.connect(DATABASE, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            active_sensors = cursor.execute(
                "SELECT id, name, type, min_threshold, max_threshold FROM sensors WHERE status = 'Actif'"
            ).fetchall()
            
            if not active_sensors:
                print("INFO [Simulation] Aucun capteur actif trouvé, en attente...")
            else:
                print(f"INFO [Simulation] Génération de données pour {len(active_sensors)} capteur(s) actif(s)...")

            for sensor in active_sensors:
                sensor_config = SENSOR_CONFIGS.get(sensor['type'])
                if not sensor_config:
                    continue

                last_data = cursor.execute(
                    "SELECT value FROM sensor_data WHERE sensor_id = ? ORDER BY timestamp DESC LIMIT 1",
                    (sensor['id'],)
                ).fetchone()

                if last_data:
                    last_value = last_data['value']
                    variation = sensor_config['variation']
                    # Tendency to return to safe range if outside
                    if last_value < sensor_config['min_safe']:
                        change = random.uniform(0, variation)
                    elif last_value > sensor_config['max_safe']:
                        change = random.uniform(-variation, 0)
                    else:
                        change = random.uniform(-variation, variation)
                    new_value = last_value + change
                else:
                    new_value = get_initial_value(sensor['type'], sensor['min_threshold'], sensor['max_threshold'])

                # Ensure value stays within absolute thresholds
                new_value = max(sensor['min_threshold'], min(sensor['max_threshold'], new_value))

                timestamp = datetime.now()
                cursor.execute(
                    "INSERT INTO sensor_data (sensor_id, value, timestamp) VALUES (?, ?, ?)",
                    (sensor['id'], new_value, timestamp)
                )

            conn.commit()
            cursor.close()
            conn.close()

        except sqlite3.OperationalError as e:
            print(f"❌ ERREUR [Simulation] Erreur de base de données : {e}")
        except Exception as e:
            print(f"❌ ERREUR [Simulation] Une erreur inattendue est survenue : {e}")
            traceback.print_exc()
        
        time.sleep(5)

if __name__ == '__main__':
    # SQLite init block intentionally skipped — the dashboard now reads from Postgres oranextdb.

    # Sensor simulator: disabled by default to avoid polluting the real DB.
    # Re-enable by setting DISABLE_SENSOR_SIMULATOR=0 (NOT recommended against a production-like DB).
    if not DISABLE_SENSOR_SIMULATOR:
        print("⚠️  [Simulation] DISABLE_SENSOR_SIMULATOR=0 — simulator thread will start (writes SQLite, not Postgres).")
        sensor_sim_thread = Thread(target=simulate_sensor_data_logging, daemon=True)
        sensor_sim_thread.start()
    else:
        print("ℹ️  [Simulation] disabled (DISABLE_SENSOR_SIMULATOR=1). Real data comes from Postgres events table.")

    # Démarrage du serveur web Flask
    print("🚀 Démarrage du serveur Flask sur http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)