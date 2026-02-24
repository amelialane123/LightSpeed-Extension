#!/usr/bin/env python3
"""
Lightspeed → Airtable export backend (multi-tenant). Used by the Chrome extension.
Each user connects their own Lightspeed + Airtable once; tokens are stored and
refreshed automatically per connection.
Run: python export_backend.py
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from dotenv import load_dotenv

load_dotenv()

try:
    from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
    from werkzeug.security import check_password_hash, generate_password_hash
except ImportError:
    print("Install Flask: pip install flask", file=sys.stderr)
    sys.exit(1)

import lightspeed_export as ls

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
SCRIPT_DIR = Path(__file__).resolve().parent
# Use CONNECTIONS_DB to point at a persistent path (e.g. Railway volume /data/connections.db)
# so connection keys and shared API keys survive deploys/restarts.
DB_PATH = Path(os.environ.get("CONNECTIONS_DB", str(SCRIPT_DIR / "connections.db")))


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                account_id TEXT NOT NULL,
                airtable_api_key TEXT NOT NULL,
                airtable_base_id TEXT NOT NULL,
                airtable_table_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        # Migration: add selected_fields if missing (SQLite has no IF NOT EXISTS for columns)
        try:
            conn.execute("ALTER TABLE connections ADD COLUMN selected_fields TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shared_keys (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                api_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connection_shared_keys (
                connection_id TEXT PRIMARY KEY,
                shared_key_id TEXT NOT NULL
            )
        """)
        conn.commit()


DEFAULT_FIELD_IDS = ["name", "cost", "price", "vendor_name", "image"]


def _create_connection(
    *,
    access_token: str,
    refresh_token: str,
    account_id: str,
    airtable_api_key: str,
    airtable_base_id: str,
    airtable_table_name: str,
) -> str:
    conn_id = str(uuid.uuid4())
    from datetime import datetime
    default_fields_json = json.dumps(DEFAULT_FIELD_IDS)
    with _get_db() as db:
        db.execute(
            """INSERT INTO connections
               (id, access_token, refresh_token, account_id, airtable_api_key, airtable_base_id, airtable_table_name, created_at, selected_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conn_id,
                access_token,
                refresh_token,
                account_id,
                airtable_api_key,
                airtable_base_id,
                airtable_table_name,
                datetime.utcnow().isoformat() + "Z",
                default_fields_json,
            ),
        )
        db.commit()
    return conn_id


def _get_connection(conn_id: str) -> sqlite3.Row | None:
    with _get_db() as db:
        row = db.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
        return row


def _update_connection_tokens(conn_id: str, access_token: str, refresh_token: str) -> None:
    with _get_db() as db:
        db.execute(
            "UPDATE connections SET access_token = ?, refresh_token = ? WHERE id = ?",
            (access_token, refresh_token, conn_id),
        )
        db.commit()


def _get_selected_fields(conn_id: str) -> list[str]:
    """Return selected field ids for connection; defaults to DEFAULT_FIELD_IDS if not set."""
    row = _get_connection(conn_id)
    if not row:
        return DEFAULT_FIELD_IDS
    raw = ""
    try:
        raw = (row["selected_fields"] or "").strip()
    except (KeyError, IndexError, TypeError):
        pass
    if not raw:
        return DEFAULT_FIELD_IDS
    try:
        ids = json.loads(raw)
        if isinstance(ids, list) and ids:
            valid = {f["id"] for f in ls.AVAILABLE_FIELDS}
            return [x for x in ids if str(x).lower() in valid] or DEFAULT_FIELD_IDS
    except (json.JSONDecodeError, TypeError):
        pass
    return DEFAULT_FIELD_IDS


def _update_connection_fields(conn_id: str, field_ids: list[str]) -> None:
    valid = {f["id"] for f in ls.AVAILABLE_FIELDS}
    filtered = [x for x in field_ids if str(x).lower() in valid]
    if not filtered:
        filtered = DEFAULT_FIELD_IDS
    with _get_db() as db:
        db.execute(
            "UPDATE connections SET selected_fields = ? WHERE id = ?",
            (json.dumps(filtered), conn_id),
        )
        db.commit()


def _update_connection_airtable_key(conn_id: str, api_key: str) -> None:
    with _get_db() as db:
        db.execute(
            "UPDATE connections SET airtable_api_key = ? WHERE id = ?",
            (api_key.strip(), conn_id),
        )
        db.commit()


def _update_connection_base(conn_id: str, airtable_base_url_or_id: str) -> bool:
    """Update connection's Airtable base. Returns True if base_id was valid and updated."""
    base_id = _extract_airtable_base_id(airtable_base_url_or_id)
    if not base_id:
        return False
    with _get_db() as db:
        db.execute(
            "UPDATE connections SET airtable_base_id = ? WHERE id = ?",
            (base_id, conn_id),
        )
        db.commit()
    return True


def _get_airtable_key_for_connection(conn_id: str) -> str | None:
    """Resolve Airtable API key: shared key (if unlocked) > connection's key > server env."""
    row = _get_connection(conn_id)
    if not row:
        return None
    with _get_db() as db:
        link = db.execute(
            "SELECT shared_key_id FROM connection_shared_keys WHERE connection_id = ?",
            (conn_id,),
        ).fetchone()
        if link:
            sk = db.execute(
                "SELECT api_key FROM shared_keys WHERE id = ?",
                (link[0],),
            ).fetchone()
            if sk and (sk[0] or "").strip():
                return sk[0].strip()
    return (row["airtable_api_key"] or "").strip() or os.environ.get("AIRTABLE_API_KEY", "").strip() or None


def _list_shared_keys() -> list[dict]:
    with _get_db() as db:
        rows = db.execute(
            "SELECT id, label, created_at FROM shared_keys ORDER BY created_at DESC"
        ).fetchall()
        return [{"id": r[0], "label": r[1], "created_at": r[2]} for r in rows]


def _create_shared_key(label: str, password: str, api_key: str) -> str:
    sk_id = str(uuid.uuid4())
    from datetime import datetime
    pwh = generate_password_hash(password, method="scrypt")
    with _get_db() as db:
        db.execute(
            """INSERT INTO shared_keys (id, label, password_hash, api_key, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (sk_id, label.strip(), pwh, api_key.strip(), datetime.utcnow().isoformat() + "Z"),
        )
        db.commit()
    return sk_id


def _verify_shared_key_password(shared_key_id: str, password: str) -> bool:
    """Verify password for a shared key without linking. Returns True if correct."""
    with _get_db() as db:
        row = db.execute(
            "SELECT password_hash FROM shared_keys WHERE id = ?",
            (shared_key_id,),
        ).fetchone()
        return bool(row and check_password_hash(row[0], password))


def _unlock_shared_key(shared_key_id: str, password: str, connection_id: str) -> bool:
    with _get_db() as db:
        row = db.execute(
            "SELECT id, password_hash FROM shared_keys WHERE id = ?",
            (shared_key_id,),
        ).fetchone()
        if not row or not check_password_hash(row[1], password):
            return False
        db.execute(
            """INSERT OR REPLACE INTO connection_shared_keys (connection_id, shared_key_id)
               VALUES (?, ?)""",
            (connection_id, shared_key_id),
        )
        db.commit()
    return True


def _ensure_fresh_tokens(conn_id: str) -> tuple[str, str]:
    """Load connection, refresh if needed, update DB, return (access_token, refresh_token)."""
    row = _get_connection(conn_id)
    if not row:
        raise ValueError("Connection not found")
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Server missing LIGHTSPEED_CLIENT_ID / LIGHTSPEED_CLIENT_SECRET")
    access_token = row["access_token"]
    refresh_token = row["refresh_token"]
    try:
        data = ls.refresh_oauth_token(refresh_token, client_id, client_secret)
        access_token = data["access_token"]
        if data.get("refresh_token"):
            refresh_token = data["refresh_token"]
        _update_connection_tokens(conn_id, access_token, refresh_token)
    except Exception as e:
        raise ValueError(
            "Lightspeed sign-in has expired or was revoked. Please reconnect: "
            "open the extension options, click Reconnect, and complete the connection flow again."
        ) from e
    return access_token, refresh_token


@app.after_request
def _cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.before_request
def _cors_preflight():
    if request.method == "OPTIONS" and request.path.startswith("/api/"):
        return "", 204


# ----- API (for extension) -----

@app.route("/api/run", methods=["OPTIONS"])
def api_run_options():
    return "", 204


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json() or {}
    connection_id = (data.get("connection_id") or "").strip()
    category_id = (data.get("category_id") or "").strip() or "ALL"
    listing_filters = data.get("listing_filters")
    if not isinstance(listing_filters, dict):
        listing_filters = {}
    if data.get("qoh_positive_only") is True:
        listing_filters["qoh_positive"] = "on"
        listing_filters["qoh_zero"] = "off"
    if not connection_id:
        return jsonify({"success": False, "error": "Missing connection_id. Add your connection key in the extension options."}), 200
    try:
        access_token, refresh_token = _ensure_fresh_tokens(connection_id)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 200
    row = _get_connection(connection_id)
    if not row:
        return jsonify({"success": False, "error": "Connection not found. Reconnect at /connect."}), 200
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        return jsonify({"success": False, "error": "Server misconfigured (missing client credentials)."}), 200
    airtable_key = _get_airtable_key_for_connection(connection_id)
    if not airtable_key:
        return jsonify({"success": False, "error": "No Airtable API key. Use an existing store key or upload your own when you connect, or add your key in extension settings."}), 200
    selected = _get_selected_fields(connection_id)
    env = {
        **os.environ,
        "LIGHTSPEED_ACCESS_TOKEN": access_token,
        "LIGHTSPEED_REFRESH_TOKEN": refresh_token,
        "LIGHTSPEED_ACCOUNT_ID": row["account_id"],
        "AIRTABLE_API_KEY": airtable_key,
        "AIRTABLE_BASE_ID": row["airtable_base_id"],
        "AIRTABLE_TABLE_NAME": row["airtable_table_name"],
        "AIRTABLE_CREATE_NEW_TABLE": "1",  # create a new table per export; push uses table ID (avoids 403 on missing "Items")
        "AIRTABLE_FIELDS": ",".join(selected),
        "FROM_EXPORT_BACKEND": "1",  # script skips opening browser; extension opens the tab
    }
    if listing_filters:
        env["EXPORT_LISTING_FILTERS"] = json.dumps(listing_filters)
    cmd = [sys.executable, str(SCRIPT_DIR / "lightspeed_export.py")]
    if category_id.upper() != "ALL":
        cmd.extend(["--category-id", category_id])
    try:
        result = subprocess.run(
            cmd,
            cwd=str(SCRIPT_DIR),
            capture_output=True,
            text=True,
            timeout=3600,
            env=env,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return jsonify({"success": False, "output": out, "error": "Export failed"}), 200
        base_id = (row["airtable_base_id"] or "").strip()
        table_match = re.search(r"AIRTABLE_TABLE_ID=(tbl[\w]+)", out)
        table_id = table_match.group(1) if table_match else None
        if base_id and table_id:
            airtable_url = f"https://airtable.com/{base_id}/{table_id}"
        elif base_id:
            airtable_url = f"https://airtable.com/{base_id}"
        else:
            airtable_url = None
        return jsonify({"success": True, "output": out, "airtable_url": airtable_url})
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Export timed out"}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


# ----- Gallery (printable / PDF-friendly view) -----

def _gallery_share_secret() -> bytes:
    """Secret for signing gallery share tokens. Use GALLERY_SHARE_SECRET or FLASK_SECRET_KEY in production so share links work across workers/restarts."""
    raw = (
        (os.environ.get("GALLERY_SHARE_SECRET") or "").strip()
        or (os.environ.get("FLASK_SECRET_KEY") or "").strip()
        or (app.secret_key or "")
    )
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1")
    return hashlib.sha256((raw or "fallback").encode()).digest()


def _create_gallery_share_token(connection_id: str, category_id: str | None) -> str:
    """Create a signed token that locks the gallery to this connection and category (or ALL)."""
    payload = connection_id + "|" + (category_id or "ALL")
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(_gallery_share_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return payload_b64 + "." + sig


def _verify_gallery_share_token(token: str) -> tuple[str, str] | None:
    """Verify token and return (connection_id, category_id_or_ALL) or None."""
    if not token or "." not in token:
        return None
    payload_b64, sig = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(payload_b64 + "==").decode()
    except Exception:
        return None
    if "|" not in payload:
        return None
    expected = hmac.new(_gallery_share_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    conn_id, cat_id = payload.split("|", 1)
    return (conn_id.strip(), cat_id.strip() or "ALL")


GALLERY_LOADING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Loading gallery…</title>
  <style>
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: #f8f9fa;
      color: #1a1a1a;
    }
    .loading-box {
      text-align: center;
      padding: 2rem;
    }
    .loading-box h2 {
      font-size: 1.25rem;
      font-weight: 600;
      margin: 0 0 1rem;
    }
    .loading-box p {
      margin: 0 0 1.5rem;
      color: #555;
    }
    .spinner {
      width: 40px;
      height: 40px;
      border: 3px solid #e0e0e0;
      border-top-color: #06c;
      border-radius: 50%;
      animation: spin 0.9s linear infinite;
      margin: 0 auto 1rem;
    }
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
  </style>
</head>
<body>
  <div class="loading-box">
    <div class="spinner" aria-hidden="true"></div>
    <h2>Loading your gallery</h2>
    <p>This may take a moment.</p>
  </div>
  <script>
    (function() {
      var shareToken = {{ share_token | tojson }};
      var fullUrl = shareToken
        ? (window.location.origin + '/gallery/full?share_token=' + encodeURIComponent(shareToken))
        : (window.location.origin + '/gallery/full' + window.location.search);
      fetch(fullUrl)
        .then(function(r) {
          if (!r.ok) throw new Error(r.statusText);
          return r.text();
        })
        .then(function(html) {
          document.open();
          document.write(html);
          document.close();
        })
        .catch(function(err) {
          document.querySelector('.loading-box').innerHTML = '<h2>Could not load gallery</h2><p>' + (err.message || 'Something went wrong.') + '</p><p><a href="' + window.location.pathname + '">Try again</a></p>';
        });
    })();
  </script>
</body>
</html>
"""


GALLERY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gallery — {{ title }}</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      margin: 1rem;
      color: #1a1a1a;
      background: #f8f9fa;
    }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    .gallery {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 1rem;
      align-items: start;
    }
    .card {
      break-inside: avoid;
      page-break-inside: avoid;
      background: #fff;
      border: 1px solid #e0e0e0;
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
      height: 380px;
    }
    .card-image-wrap {
      position: relative;
      flex-shrink: 0;
      width: 100%;
      height: 220px;
      padding: 8px;
      background: #f5f5f5;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card-carousel-inner {
      position: absolute;
      inset: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card-carousel-slide {
      display: none;
      width: 100%;
      height: 100%;
    }
    .card-carousel-slide.active {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card-carousel-slide img {
      max-width: 100%;
      max-height: 100%;
      width: auto;
      height: auto;
      object-fit: contain;
      border-radius: 4px;
    }
    .card-carousel-nav {
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      width: 100%;
      display: flex;
      justify-content: space-between;
      padding: 0 4px;
      pointer-events: none;
    }
    .card-carousel-nav button {
      pointer-events: auto;
      width: 28px;
      height: 28px;
      border: none;
      border-radius: 50%;
      background: rgba(0,0,0,0.5);
      color: #fff;
      font-size: 16px;
      line-height: 1;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 0;
    }
    .card-carousel-nav button:hover {
      background: rgba(0,0,0,0.7);
    }
    .card-image-wrap .no-image {
      color: #999;
      font-size: 12px;
    }
    .card-details {
      padding: 10px;
      flex: 1;
      min-width: 0;
      min-height: 0;
      overflow-y: auto;
    }
    .card-detail {
      font-size: 13px;
      margin-bottom: 6px;
      word-wrap: break-word;
      overflow-wrap: break-word;
      word-break: break-word;
    }
    .card-detail .label {
      font-weight: 600;
      color: #555;
      margin-right: 4px;
    }
    .card-detail:last-child { margin-bottom: 0; }
    .gallery.cols-2 { grid-template-columns: repeat(2, 1fr); }
    .gallery.cols-3 { grid-template-columns: repeat(3, 1fr); }
    .gallery.cols-4 { grid-template-columns: repeat(4, 1fr); }
    .gallery.cols-5 { grid-template-columns: repeat(5, 1fr); }
    .cards-per-row-wrap {
      margin-bottom: 1rem;
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .cards-per-row-wrap label { font-size: 14px; font-weight: 500; margin: 0; }
    .cards-per-row-wrap .btn-group { display: flex; gap: 4px; }
    .cards-per-row-wrap .layout-btn {
      padding: 6px 12px;
      font-size: 13px;
      border: 1px solid #ccc;
      background: #fff;
      border-radius: 6px;
      cursor: pointer;
    }
    .cards-per-row-wrap .layout-btn:hover { background: #f0f0f0; }
    .cards-per-row-wrap .layout-btn.active { background: #06c; color: #fff; border-color: #06c; }
    .share-popup-item {
      display: block;
      width: 100%;
      padding: 10px 14px;
      text-align: left;
      font-size: 14px;
      background: none;
      border: none;
      cursor: pointer;
      color: #1a1a1a;
    }
    .share-popup-item:hover { background: #f0f0f0; }
    .share-popup-item:first-child { border-radius: 8px 8px 0 0; }
    .share-popup-item:last-child { border-radius: 0 0 8px 8px; }
    @media print {
      body { margin: 0; background: #fff; }
      .share-export-wrap { display: none !important; }
      .cards-per-row-wrap { display: none !important; }
      h1 { margin-bottom: 0.5rem; }
      .gallery { gap: 1rem; }
      .card {
        height: 380px !important;
        min-height: 380px !important;
        box-shadow: none;
        border: 1px solid #ccc;
        break-inside: avoid;
        page-break-inside: avoid;
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }
      .card-image-wrap {
        height: 220px !important;
        min-height: 220px !important;
        flex-shrink: 0;
        background: #f5f5f5 !important;
        padding: 8px;
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }
      .card-details {
        padding: 12px 10px;
        flex: 1;
        min-height: 0;
      }
      .card-carousel-nav { display: none !important; }
      .card-carousel-slide { display: none !important; }
      .card-carousel-slide:first-child { display: flex !important; align-items: center; justify-content: center; }
    }
  </style>
</head>
<body>
  <h1>{{ title }}</h1>
  <p class="muted" style="margin-bottom:1rem;color:#666;">{{ items|length }} item(s). Click arrows to change image. Print or Save as PDF shows first image only; cards will not be cut across pages.</p>
  {% if share_url %}
  <div class="share-export-wrap" style="margin-bottom:1rem;position:relative;">
    <button type="button" id="share-export-btn" style="padding:8px 16px;font-size:14px;font-weight:500;color:#fff;background:#06c;border:none;border-radius:6px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;">Share / Export &#9662;</button>
    <div id="share-export-popup" class="share-popup" style="display:none;position:absolute;top:100%;left:0;margin-top:6px;min-width:200px;background:#fff;border:1px solid #ddd;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.15);z-index:100;padding:6px 0;">
      <button type="button" class="share-popup-item" data-action="copy">Copy link</button>
      <button type="button" class="share-popup-item" data-action="print">Print / Save as PDF</button>
      <button type="button" class="share-popup-item" data-action="csv">Download CSV</button>
    </div>
  </div>
  {% endif %}
  <div class="cards-per-row-wrap">
    <label for="layout-btns">Cards per row (for print):</label>
    <div class="btn-group" id="layout-btns" role="group">
      <button type="button" class="layout-btn active" data-cols="auto">Auto</button>
      <button type="button" class="layout-btn" data-cols="2">2</button>
      <button type="button" class="layout-btn" data-cols="3">3</button>
      <button type="button" class="layout-btn" data-cols="4">4</button>
      <button type="button" class="layout-btn" data-cols="5">5</button>
    </div>
  </div>
  <div class="gallery" id="gallery-grid">
    {% for item in items %}
    <div class="card" data-card-index="{{ loop.index0 }}">
      <div class="card-image-wrap">
        {% if item.image_urls_list %}
        <div class="card-carousel-inner">
          {% for url in item.image_urls_list %}
          <div class="card-carousel-slide {{ 'active' if loop.first else '' }}" data-slide-index="{{ loop.index0 }}">
            <img src="{{ url }}" alt="" loading="lazy">
          </div>
          {% endfor %}
        </div>
        {% if item.image_urls_list|length > 1 %}
        <div class="card-carousel-nav" aria-hidden="true">
          <button type="button" class="carousel-prev" title="Previous image">&lsaquo;</button>
          <button type="button" class="carousel-next" title="Next image">&rsaquo;</button>
        </div>
        {% endif %}
        {% else %}
        <span class="no-image">No image</span>
        {% endif %}
      </div>
      <div class="card-details">
        {% for field in fields %}
        {% if field.rowKey != 'image_urls' and field.rowKey != 'image' %}
        {% set val = item.get(field.rowKey) %}
        {% if val is not none and val != '' %}
        <div class="card-detail"><span class="label">{{ field.displayName }}:</span> {{ val }}</div>
        {% endif %}
        {% endif %}
        {% endfor %}
      </div>
    </div>
    {% endfor %}
  </div>
  {% if share_url %}
  <script>
    window.GALLERY_SHARE_URL = {{ share_url | tojson }};
    window.GALLERY_ITEMS = {{ items | tojson }};
    window.GALLERY_FIELDS = {{ fields | tojson }};
  </script>
  {% endif %}
  <script>
    document.querySelectorAll('.card').forEach(function(card) {
      var nav = card.querySelector('.card-carousel-nav');
      if (!nav) return;
      var inner = card.querySelector('.card-carousel-inner');
      var slides = inner ? inner.querySelectorAll('.card-carousel-slide') : [];
      var n = slides.length;
      if (n <= 1) return;
      var prevBtn = nav.querySelector('.carousel-prev');
      var nextBtn = nav.querySelector('.carousel-next');
      var current = 0;
      function goTo(i) {
        current = (i + n) % n;
        slides.forEach(function(s, j) { s.classList.toggle('active', j === current); });
      }
      prevBtn.addEventListener('click', function() { goTo(current - 1); });
      nextBtn.addEventListener('click', function() { goTo(current + 1); });
    });
    var galleryEl = document.getElementById('gallery-grid');
    document.querySelectorAll('.layout-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        document.querySelectorAll('.layout-btn').forEach(function(b) { b.classList.remove('active'); });
        this.classList.add('active');
        var cols = this.getAttribute('data-cols');
        if (galleryEl) {
          galleryEl.classList.remove('cols-2', 'cols-3', 'cols-4', 'cols-5');
          if (cols !== 'auto') galleryEl.classList.add('cols-' + cols);
        }
      });
    });
    var shareBtn = document.getElementById('share-export-btn');
    var popup = document.getElementById('share-export-popup');
    if (shareBtn && popup) {
      shareBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        popup.style.display = popup.style.display === 'none' ? 'block' : 'none';
      });
      document.addEventListener('click', function() { popup.style.display = 'none'; });
      popup.addEventListener('click', function(e) { e.stopPropagation(); });
      popup.querySelectorAll('.share-popup-item').forEach(function(btn) {
        btn.addEventListener('click', function() {
          var action = this.getAttribute('data-action');
          popup.style.display = 'none';
          if (action === 'copy' && window.GALLERY_SHARE_URL) {
            navigator.clipboard.writeText(window.GALLERY_SHARE_URL).then(function() {
              shareBtn.textContent = 'Copied!';
              setTimeout(function() { shareBtn.innerHTML = 'Share / Export &#9662;'; }, 1500);
            });
          } else if (action === 'print') {
            window.print();
          } else if (action === 'csv' && window.GALLERY_ITEMS && window.GALLERY_FIELDS) {
            var fields = window.GALLERY_FIELDS.filter(function(f) { return f.rowKey !== 'image_urls' && f.rowKey !== 'image'; });
            var header = fields.map(function(f) { return '"' + (f.displayName || f.rowKey || '').replace(/"/g, '""') + '"'; }).join(',');
            var rows = window.GALLERY_ITEMS.map(function(item) {
              return fields.map(function(f) {
                var v = item[f.rowKey];
                if (v == null) v = '';
                v = String(v).replace(/"/g, '""');
                return '"' + v + '"';
              }).join(',');
            });
            var csv = [header].concat(rows).join("\\n");
            var blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
            var a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'gallery-export.csv';
            a.click();
            URL.revokeObjectURL(a.href);
          }
        });
      });
    }
  </script>
</body>
</html>
"""


GALLERY_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Connection not found</title></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:3rem auto;padding:1rem;">
  <h1>Connection not found</h1>
  <p>Your connection key is no longer in our database. This often happens after the server restarts (e.g. on Railway) if the database isn’t stored on persistent disk.</p>
  <p><strong>Fix:</strong> Reconnect once to create a new connection, then use the extension again.</p>
  <p><a href="/connect" style="display:inline-block;padding:10px 16px;background:#06c;color:#fff;text-decoration:none;border-radius:6px;">Reconnect now</a></p>
  <p style="color:#666;font-size:14px;">Or open the extension options and click “Reconnect”.</p>
</body>
</html>
"""

GALLERY_SHARE_ERROR_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Share link unavailable</title></head>
<body style="font-family:system-ui,sans-serif;max-width:480px;margin:3rem auto;padding:1rem;">
  <h1>Could not load this gallery link</h1>
  <p>This share link is invalid or expired. If you just copied it, the server may need a fixed secret so links work across restarts.</p>
  <p><strong>If you're the owner:</strong> Set <code>FLASK_SECRET_KEY</code> or <code>GALLERY_SHARE_SECRET</code> in your backend environment (e.g. Railway variables), then open the gallery again and copy a new link.</p>
  <p><a href="/connect" style="display:inline-block;padding:10px 16px;background:#06c;color:#fff;text-decoration:none;border-radius:6px;">Go to connect</a></p>
</body>
</html>
"""


def _get_gallery_data(
    key: str,
    category_id: str | None,
    listing_filters: dict | None = None,
) -> tuple[list[dict], list[dict], str]:
    """Load gallery rows, fields, and title for the given connection and category. Raises on error."""
    row = _get_connection(key)
    if not row:
        raise ValueError("Connection not found")
    access_token, refresh_token = _ensure_fresh_tokens(key)
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("Server misconfigured")
    session = ls.SessionWithRefresh(access_token, refresh_token, client_id, client_secret)
    selected = _get_selected_fields(key)
    rows = ls.export_items(
        session,
        row["account_id"],
        load_relations=[],
        include_images=True,
        category_id=category_id,
        field_ids=selected,
        listing_filters=listing_filters or {},
    )
    for r in rows:
        r["image_urls_list"] = [u.strip() for u in (r.get("image_urls") or "").split("|") if u.strip()]
    fields = ls._fields_for_ids(selected)
    if category_id:
        try:
            title = ls.get_category_name(session, row["account_id"], category_id) or f"Category {category_id}"
        except Exception:
            title = f"Category {category_id}"
    else:
        title = "All items"
    return (rows, fields, title)


def _render_gallery_full(
    key: str,
    category_id: str | None,
    listing_filters: dict,
    share_url: str | None = None,
) -> str:
    """Load gallery data and return full gallery HTML. Used by /gallery/full."""
    row = _get_connection(key)
    if not row:
        return ""
    access_token, refresh_token = _ensure_fresh_tokens(key)
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        return ""
    rows, fields, title = _get_gallery_data(key, category_id, listing_filters=listing_filters)
    return render_template_string(
        GALLERY_HTML, items=rows, fields=fields, title=title, share_url=share_url or ""
    )


@app.route("/gallery")
def gallery_page():
    """Returns loading shell immediately; client fetches /gallery/full for actual content."""
    key = (request.args.get("key") or "").strip()
    if not key:
        return redirect(url_for("connect_page"))
    if not _get_connection(key):
        return redirect(url_for("connect_page"))
    return render_template_string(
        GALLERY_LOADING_HTML,
        share_token=None,
    )


@app.route("/gallery/full")
def gallery_full():
    """Heavy gallery render (called by loading page via fetch)."""
    key = (request.args.get("key") or "").strip()
    share_token = (request.args.get("share_token") or "").strip()
    if share_token:
        parsed = _verify_gallery_share_token(share_token)
        if not parsed:
            return render_template_string(GALLERY_SHARE_ERROR_HTML), 200
        key, category_param = parsed
        category_id = None if category_param.upper() == "ALL" else category_param
        listing_filters = {}
        share_url = url_for("gallery_share", token=share_token, _external=True)
    else:
        category_id_param = (request.args.get("category_id") or "").strip()
        category_id = category_id_param if category_id_param and category_id_param.upper() != "ALL" else None
        listing_filters = {}
        try:
            raw = (request.args.get("listing_filters") or "").strip()
            if raw:
                listing_filters = json.loads(raw)
                if not isinstance(listing_filters, dict):
                    listing_filters = {}
        except (json.JSONDecodeError, TypeError):
            pass
        if (request.args.get("qoh_positive_only") or "").strip().lower() in ("1", "true", "yes"):
            listing_filters["qoh_positive"] = "on"
            listing_filters["qoh_zero"] = "off"
            shop = (request.args.get("shop_id") or "").strip()
            if shop and shop != "-1":
                listing_filters["shop_id"] = shop
        share_url = None
        try:
            token = _create_gallery_share_token(key, category_id)
            share_url = url_for("gallery_share", token=token, _external=True)
        except Exception:
            pass
    if not key:
        return "Missing key.", 400
    try:
        html = _render_gallery_full(key, category_id, listing_filters, share_url=share_url)
    except ValueError:
        if share_token:
            return render_template_string(GALLERY_SHARE_ERROR_HTML), 200
        return render_template_string(GALLERY_ERROR_HTML), 404
    except Exception as e:
        return f"Failed to load items: {e}", 500
    if not html:
        if share_token:
            return render_template_string(GALLERY_SHARE_ERROR_HTML), 200
        return render_template_string(GALLERY_ERROR_HTML), 404
    return html


@app.route("/gallery/s/<token>")
def gallery_share(token: str):
    """Returns loading shell; client fetches /gallery/full?share_token= for actual content."""
    parsed = _verify_gallery_share_token(token)
    if not parsed:
        return render_template_string(GALLERY_ERROR_HTML), 404
    if not _get_connection(parsed[0]):
        return render_template_string(GALLERY_ERROR_HTML), 404
    return render_template_string(
        GALLERY_LOADING_HTML,
        share_token=token,
    )


# ----- Connect (multi-tenant OAuth + setup) -----

def _oauth_redirect_uri() -> str:
    """Redirect URI for OAuth. Use HTTPS (Lightspeed only allows https)."""
    uri = (os.environ.get("LIGHTSPEED_REDIRECT_URI") or "").strip()
    if uri:
        if not uri.startswith("http://") and not uri.startswith("https://"):
            uri = "https://" + uri
        return uri.rstrip("/")
    base = (os.environ.get("BACKEND_PUBLIC_URL") or "").strip().rstrip("/")
    if base:
        if not base.startswith("http://") and not base.startswith("https://"):
            base = "https://" + base
        return f"{base}/connect/callback"
    # Behind a proxy (e.g. Railway): use X-Forwarded headers so we get https and correct host
    try:
        proto = (request.headers.get("X-Forwarded-Proto") or "https").strip().lower() or "https"
        host = (request.headers.get("X-Forwarded-Host") or request.host or "").strip()
        if host:
            return f"{proto}://{host}/connect/callback"
    except Exception:
        pass
    root = request.url_root.rstrip("/")
    if root and not root.startswith("http://") and not root.startswith("https://"):
        root = "https://" + root
    return root + "/connect/callback"


def _extract_airtable_base_id(value: str) -> str | None:
    """Extract Airtable base ID from a full URL or return the value if it's already a base ID."""
    value = (value or "").strip()
    if not value:
        return None
    # Already looks like a base ID (app + alphanumeric)
    if value.startswith("app") and len(value) >= 14 and value[3:].replace("_", "").isalnum():
        return value
    # Try to parse as URL (e.g. https://airtable.com/appXXX/... or https://airtable.com/appXXX)
    if "airtable.com" in value:
        try:
            parsed = urlparse(value if "://" in value else "https://" + value)
            path = (parsed.path or "").strip("/")
            segments = [s for s in path.split("/") if s]
            for seg in segments:
                if seg.startswith("app") and len(seg) >= 14:
                    return seg
        except Exception:
            pass
    return None


CONNECT_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Connect Lightspeed & Airtable</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    h2 { font-size: 1rem; margin-top: 1.25rem; margin-bottom: 0.5rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    label { display: block; margin-top: 0.75rem; }
    input, select { width: 100%; padding: 0.5rem; margin: 0.25rem 0; box-sizing: border-box; }
    button, .btn { display: inline-block; padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.75rem; }
    button:hover, .btn:hover { background: #080; }
    .muted { color: #666; font-size: 0.9rem; }
    details { margin-top: 0.5rem; margin-bottom: 0.75rem; }
    details summary { cursor: pointer; color: #06c; }
    .token-steps { margin: 0.75rem 0; padding-left: 1.25rem; }
    .token-steps li { margin: 0.35rem 0; }
    .connect-option { border: 1px solid #ddd; border-radius: 6px; padding: 1rem; margin-bottom: 1rem; }
  </style>
</head>
<body>
  <h1>Connect your Lightspeed & Airtable</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <p class="muted">You must use an Airtable API key to connect. Choose one: <strong>use an existing store key</strong> (from the dropdown) or <strong>upload your own</strong> key. You only need to do this once. Exports will create new tables in the Airtable base you link.</p>

  <div class="connect-option">
    <h2>Use an existing store key</h2>
    <p class="muted">Your store already set up a shared Airtable key. Pick it from the dropdown, enter the password your team gave you, then on the next step add your Lightspeed account and Airtable base.</p>
    <form method="post" action="/connect/verify-shared-key">
      <label>Store key</label>
      <select name="shared_key_id" required>
        <option value="">— Choose a key —</option>
        {% for sk in shared_keys %}
        <option value="{{ sk.id }}">{{ sk.label }}</option>
        {% endfor %}
      </select>
      {% if not shared_keys %}
      <p class="muted">No store keys available yet. Someone with an API key needs to upload one (see below) and give it a name and password, or <a href="/shared-keys/create">create a shared key here</a>.</p>
      {% endif %}
      <label>Password for this key</label>
      <input type="password" name="password" placeholder="Enter the password from your team" required autocomplete="off">
      <button type="submit" {% if not shared_keys %}disabled{% endif %}>Continue with this key</button>
    </form>
  </div>

  <div class="connect-option">
    <h2>Upload your own API key</h2>
    <p class="muted">Paste your Airtable personal access token and the link to your base. You need your own key; there is no default key.</p>
    <form method="post" action="/connect/start" id="f">
      <label>API key <strong>(required)</strong></label>
      <p class="muted">Paste your Airtable token. <a href="https://airtable.com/create/tokens" target="_blank" rel="noopener">Create a token</a> if you don't have one.</p>
      <input type="password" name="airtable_api_key" placeholder="Paste your Airtable token (pat...)" required autocomplete="off">
      <details>
        <summary>How to create an Airtable token</summary>
        <div class="muted" style="margin-top: 0.75rem;">
          <p style="margin-bottom: 0.5rem;"><strong>Part 1 — Account and plan</strong></p>
          <ol class="token-steps" style="margin: 0.25rem 0 0.75rem 1.25rem; padding-left: 0.5rem;">
            <li>If you don’t have an Airtable account, go to <a href="https://airtable.com" target="_blank" rel="noopener">airtable.com</a> and sign up (free).</li>
            <li>Create or open a base (spreadsheet) where you want your Lightspeed exports to go.</li>
            <li>Personal access tokens and all required scopes are available on the <strong>Free</strong> plan. Free supports the same scopes as Plus, Pro, and Enterprise; the main limit is 1,000 API calls per month. If you’re on a team, your admin may need to allow API access in the workspace.</li>
          </ol>
          <p style="margin-bottom: 0.5rem;"><strong>Part 2 — Create the token</strong></p>
          <ol class="token-steps" style="margin: 0.25rem 0 0.75rem 1.25rem; padding-left: 0.5rem;">
            <li>Open <a href="https://airtable.com/create/tokens" target="_blank" rel="noopener">Airtable: Create a token</a> (you must be signed in).</li>
            <li>Click <strong>Create new token</strong>. Give it a name (e.g. “Lightspeed export”).</li>
            <li>Click <strong>Add a scope</strong>. Add these four scopes (one at a time or as your interface allows):<br>
              <code>data.records:read</code>, <code>data.records:write</code>, <code>schema.bases:read</code>, <code>schema.bases:write</code>.</li>
            <li>Click <strong>Add a base</strong>. Choose the base (or “All bases in workspace”) where you want exports to go. The token must have access to the base you’ll paste in “Link to your Airtable” below.</li>
            <li>Click <strong>Create token</strong>. Copy the token (it starts with <code>pat</code>) and paste it into the API key field above. Keep it private; anyone with the token can access that base.</li>
          </ol>
          <p style="margin-top: 0.5rem;">More: <a href="https://airtable.com/developers/web/guides/personal-access-tokens" target="_blank" rel="noopener">Personal access tokens</a> · <a href="https://airtable.com/developers/web/api/scopes" target="_blank" rel="noopener">Scopes reference</a></p>
        </div>
      </details>
      <label>Key label <span class="muted">(optional; e.g. &quot;Store Main&quot;—share with team so they see this name in the dropdown)</span></label>
      <input type="text" name="share_label" placeholder="e.g. Store Main">
      <label>Key password <span class="muted">(optional; team members enter this to unlock and use this key)</span></label>
      <input type="password" name="share_password" placeholder="Choose a password for your team" autocomplete="new-password">
      <label>Lightspeed Account ID <span class="muted">(find in your Lightspeed URL or settings)</span></label>
      <input type="text" name="account_id" placeholder="e.g. 12345" required>
      <label>Link to your Airtable</label>
      <p class="muted">Open the Airtable where you want exports to go, then copy the URL from your browser and paste it below.</p>
      <input type="text" name="airtable_base_url" placeholder="Paste the link when your Airtable is open" required>
      <button type="submit">Continue to Lightspeed authorization</button>
    </form>
  </div>

  <p class="muted" style="margin-top: 1.5rem;">To create a shared key without connecting (e.g. for someone else to upload the key): <a href="/shared-keys/create">Create a shared store key</a>.</p>
</body>
</html>
"""

CONNECT_ENTER_DETAILS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Add your Lightspeed &amp; Airtable base</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    .muted { color: #666; font-size: 0.9rem; }
    label { display: block; margin-top: 0.75rem; }
    input { width: 100%; padding: 0.5rem; margin: 0.25rem 0; box-sizing: border-box; }
    button { padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.75rem; }
    button:hover { background: #080; }
  </style>
</head>
<body>
  <h1>Add your Lightspeed &amp; Airtable base</h1>
  <p class="muted">You're using the store key <strong>{{ shared_key_label }}</strong>. Enter your Lightspeed Account ID and the link to the Airtable base where you want exports to go (must be a base that the store key can access).</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <form method="post" action="/connect/start">
    <label>Lightspeed Account ID</label>
    <input type="text" name="account_id" placeholder="e.g. 12345" required>
    <label>Link to your Airtable</label>
    <input type="text" name="airtable_base_url" placeholder="Paste the Airtable base URL" required>
    <button type="submit">Continue to Lightspeed authorization</button>
  </form>
  <p class="muted" style="margin-top: 1rem;"><a href="/connect">Back</a></p>
</body>
</html>
"""

SHARED_KEY_CREATE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Create a shared store key</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    .saved { color: #080; margin: 0.5rem 0; }
    label { display: block; margin-top: 0.75rem; }
    input { width: 100%; padding: 0.5rem; margin: 0.25rem 0; box-sizing: border-box; }
    button { padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.75rem; }
    button:hover { background: #080; }
    .muted { color: #666; font-size: 0.9rem; }
    a { color: #06c; }
  </style>
</head>
<body>
  <h1>Create a shared store key</h1>
  <p class="muted">Upload an Airtable API key with a screen name and password. Anyone at your store can then select this key in the extension and unlock it with the password to use the same Airtable base.</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  {% if saved %}<p class="saved">Shared key created. Share the screen name and password with your team so they can select it in the extension and unlock it.</p>{% endif %}
  <form method="post" action="/shared-keys/create">
    <label>Screen name <span class="muted">(what others will see when they search, e.g. "Store Main")</span></label>
    <input type="text" name="label" placeholder="e.g. Store Main" required>
    <label>Password <span class="muted">(team members will enter this to unlock and use the key)</span></label>
    <input type="password" name="password" placeholder="Choose a password" required autocomplete="new-password">
    <label>Airtable API key</label>
    <input type="password" name="api_key" placeholder="Paste your Airtable personal access token (pat...)" required autocomplete="off">
    <button type="submit">Create shared key</button>
  </form>
  <p class="muted" style="margin-top: 1.5rem;"><a href="/connect">Back to Connect</a></p>
</body>
</html>
"""

CONNECT_PASTE_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Paste redirect URL</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    label { display: block; margin-top: 0.75rem; }
    input { width: 100%; padding: 0.5rem; margin: 0.25rem 0; box-sizing: border-box; }
    button { padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.75rem; }
    button:hover { background: #080; }
    .muted { color: #666; font-size: 0.9rem; }
    a { color: #06c; }
  </style>
</head>
<body>
  <h1>Sign in with Lightspeed</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <p>1. Click the link below to open Lightspeed and authorize the app.</p>
  <p><a href="{{ auth_url }}" target="_blank" rel="noopener">Open Lightspeed login</a></p>
  <p>2. After authorizing, you'll be redirected. <strong>Copy the full URL from your browser's address bar.</strong></p>
  <p>3. Paste it here and click Submit.</p>
  <form method="post" action="/connect/paste">
    <input type="url" name="redirect_url" placeholder="Paste the full redirect URL here (e.g. https://oauth.pstmn.io/...?code=...)" required>
    <button type="submit">Submit</button>
  </form>
  <p class="muted">The page after redirect may be blank; the URL still contains the code we need.</p>
</body>
</html>
"""

SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Connected</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; color: #080; }
    .muted { color: #666; font-size: 0.9rem; margin-top: 1rem; }
    a { color: #06c; }
  </style>
</head>
<body>
  <h1>You're all set</h1>
  <p>You've successfully connected Lightspeed and Airtable. The extension has saved your connection.</p>
  <p><a href="/settings?key={{ connection_key }}">Configure which fields to export</a></p>
  <p class="muted">Fields to export are the item details that get sent to Airtable when you run an export—for example name, price, cost, vendor, and category. You can change this selection anytime from the link above or in the extension options.</p>
</body>
</html>
"""

SETTINGS_HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Export fields – Lightspeed → Airtable</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 520px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    .saved { color: #080; margin: 0.5rem 0; }
    .field-list { list-style: none; padding: 0; margin: 1rem 0; }
    .field-list li { margin: 0.5rem 0; }
    .field-list input { margin-right: 0.5rem; }
    button { padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.5rem; }
    button:hover { background: #080; }
    .muted { color: #666; font-size: 0.9rem; }
    a { color: #06c; }
    details { margin-top: 0.5rem; margin-bottom: 0.75rem; }
    details summary { cursor: pointer; color: #06c; }
    .token-steps { margin: 0.75rem 0; padding-left: 1.25rem; }
    .token-steps li { margin: 0.35rem 0; }
  </style>
</head>
<body>
  <h1>Choose export fields</h1>
  <p>Select which fields to include when exporting to Airtable. These settings are saved for this connection.</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  {% if saved %}<p class="saved">Settings saved.</p>{% endif %}
  <form method="post" action="/settings">
    <input type="hidden" name="key" value="{{ key }}">
    <label>Airtable personal API key <span class="muted">(optional)</span></label>
    <p class="muted">Use your own token if your base is in a different workspace. Leave blank to keep your current key.</p>
    <input type="password" name="airtable_api_key" placeholder="Paste a new Airtable token here" autocomplete="off" style="margin-bottom: 0.25rem;">
    <details>
      <summary>Don't have a token? How to create one</summary>
      <div class="muted" style="margin-top: 0.75rem;">
        <p style="margin-bottom: 0.5rem;"><strong>Part 1 — Account and plan</strong></p>
        <ol class="token-steps" style="margin: 0.25rem 0 0.75rem 1.25rem; padding-left: 0.5rem;">
          <li>If you don’t have an Airtable account, go to <a href="https://airtable.com" target="_blank" rel="noopener">airtable.com</a> and sign up (free).</li>
          <li>Create or open a base where you want your Lightspeed exports to go.</li>
          <li>All required scopes work on the <strong>Free</strong> plan (Free has a 1,000 API calls/month limit). On a team, your admin may need to allow API access.</li>
        </ol>
        <p style="margin-bottom: 0.5rem;"><strong>Part 2 — Create the token</strong></p>
        <ol class="token-steps" style="margin: 0.25rem 0 0.75rem 1.25rem; padding-left: 0.5rem;">
          <li>Open <a href="https://airtable.com/create/tokens" target="_blank" rel="noopener">Airtable: Create a token</a> (signed in).</li>
          <li>Click <strong>Create new token</strong> and name it (e.g. “Lightspeed export”).</li>
          <li>Click <strong>Add a scope</strong> and add: <code>data.records:read</code>, <code>data.records:write</code>, <code>schema.bases:read</code>, <code>schema.bases:write</code>.</li>
          <li>Click <strong>Add a base</strong> and choose the base (or workspace) for exports.</li>
          <li>Click <strong>Create token</strong>, copy the token (starts with <code>pat</code>), and paste it above. Keep it private.</li>
        </ol>
        <p style="margin-top: 0.5rem;"><a href="https://airtable.com/developers/web/guides/personal-access-tokens" target="_blank" rel="noopener">Personal access tokens</a> · <a href="https://airtable.com/developers/web/api/scopes" target="_blank" rel="noopener">Scopes reference</a></p>
      </div>
    </details>
    <ul class="field-list">
      {% for f in available_fields %}
      <li>
        <label>
          <input type="checkbox" name="field" value="{{ f.id }}" {{ 'checked' if f.id in selected_ids else '' }}>
          {{ f.displayName }}
        </label>
      </li>
      {% endfor %}
    </ul>
    <button type="submit">Save</button>
  </form>
  <p class="muted">At least one field must be selected. Default: Name, Cost, Price, Vendor Name, Image.</p>
</body>
</html>
"""


@app.route("/")
def index():
    return jsonify({
        "ok": True,
        "message": "Lightspeed → Airtable backend (multi-tenant). Use /connect to connect your account; use the extension with your connection key.",
    }), 200


@app.route("/connect", methods=["GET"])
def connect_page():
    shared_keys = _list_shared_keys()
    return render_template_string(CONNECT_HTML, shared_keys=shared_keys, error=request.args.get("error"))


@app.route("/connect/verify-shared-key", methods=["POST"])
def connect_verify_shared_key():
    shared_key_id = (request.form.get("shared_key_id") or "").strip()
    password = request.form.get("password") or ""
    if not shared_key_id or not password:
        return redirect(url_for("connect_page", error="Choose a store key and enter the password."))
    if not _verify_shared_key_password(shared_key_id, password):
        return redirect(url_for("connect_page", error="Wrong password for this store key."))
    shared_keys = _list_shared_keys()
    label = next((k["label"] for k in shared_keys if k["id"] == shared_key_id), shared_key_id)
    session["shared_key_pending"] = {
        "shared_key_id": shared_key_id,
        "shared_key_password": password,
        "shared_key_label": label,
    }
    return redirect(url_for("connect_enter_details"))


@app.route("/connect/enter-details", methods=["GET"])
def connect_enter_details():
    pending = session.get("shared_key_pending") or {}
    if not pending.get("shared_key_id"):
        return redirect(url_for("connect_page"))
    return render_template_string(
        CONNECT_ENTER_DETAILS_HTML,
        shared_key_id=pending["shared_key_id"],
        shared_key_label=pending.get("shared_key_label", "Store key"),
        error=request.args.get("error"),
    )


@app.route("/connect/start", methods=["POST"])
def connect_start():
    account_id = (request.form.get("account_id") or "").strip()
    airtable_input = (
        (request.form.get("airtable_base_url") or request.form.get("airtable_base_id") or "").strip()
    )
    airtable_base_id = _extract_airtable_base_id(airtable_input)
    airtable_table_name = (request.form.get("airtable_table_name") or "").strip() or "Items"
    if not account_id or not airtable_base_id:
        shared_keys = _list_shared_keys()
        return render_template_string(
            CONNECT_HTML,
            shared_keys=shared_keys,
            error="Please fill in Account ID and paste the link to your Airtable.",
        )
    airtable_api_key = (request.form.get("airtable_api_key") or "").strip()
    shared_key_pending = session.pop("shared_key_pending", None)
    if not shared_key_pending and not airtable_api_key:
        shared_keys = _list_shared_keys()
        return render_template_string(
            CONNECT_HTML,
            shared_keys=shared_keys,
            error="Provide your Airtable API key: use an existing store key from the dropdown above, or paste your own token in the “Upload your own API key” section.",
        )
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        shared_keys = _list_shared_keys()
        return render_template_string(CONNECT_HTML, shared_keys=shared_keys, error="Server missing Lightspeed client credentials.")
    redirect_uri = _oauth_redirect_uri()
    state = secrets.token_urlsafe(24)
    share_label = (request.form.get("share_label") or "").strip()
    share_password = request.form.get("share_password") or ""
    pending_connect = {
        "state": state,
        "redirect_uri": redirect_uri,
        "account_id": account_id,
        "airtable_base_id": airtable_base_id,
        "airtable_table_name": airtable_table_name,
        "airtable_api_key": airtable_api_key,
    }
    if shared_key_pending and shared_key_pending.get("shared_key_id"):
        pending_connect["shared_key_id"] = shared_key_pending["shared_key_id"]
        pending_connect["shared_key_password"] = shared_key_pending.get("shared_key_password", "")
        pending_connect["airtable_api_key"] = ""  # use shared key after link
    if airtable_api_key and share_label and share_password:
        pending_connect["create_shared_key_after_connect"] = {
            "label": share_label,
            "password": share_password,
            "api_key": airtable_api_key,
        }
    session["pending_connect"] = pending_connect
    auth_url = (
        f"{ls.AUTHORIZE_URL}?response_type=code&client_id={quote(client_id, safe='')}"
        f"&scope=employee:all&state={quote(state, safe='')}&redirect_uri={quote(redirect_uri, safe='')}"
    )
    return redirect(auth_url)


@app.route("/connect/paste", methods=["GET", "POST"])
def connect_paste():
    pending = session.get("pending_connect") or {}
    if not pending:
        return redirect(url_for("connect_page"))
    if request.method == "GET":
        client_id = ls.env("LIGHTSPEED_CLIENT_ID")
        redirect_uri = pending.get("redirect_uri", _oauth_redirect_uri())
        auth_url = (
            f"{ls.AUTHORIZE_URL}?response_type=code&client_id={quote(client_id, safe='')}"
            f"&scope=employee:all&state={quote(pending.get('state', ''), safe='')}&redirect_uri={quote(redirect_uri, safe='')}"
        )
        return render_template_string(CONNECT_PASTE_HTML, auth_url=auth_url)
    redirect_url = (request.form.get("redirect_url") or "").strip()
    if not redirect_url:
        client_id = ls.env("LIGHTSPEED_CLIENT_ID")
        redirect_uri = pending.get("redirect_uri", _oauth_redirect_uri())
        auth_url = (
            f"{ls.AUTHORIZE_URL}?response_type=code&client_id={quote(client_id, safe='')}"
            f"&scope=employee:all&state={quote(pending.get('state', ''), safe='')}&redirect_uri={quote(redirect_uri, safe='')}"
        )
        return render_template_string(
            CONNECT_PASTE_HTML,
            auth_url=auth_url,
            error="Please paste the full redirect URL.",
        )
    parsed = urlparse(redirect_url)
    if not parsed.query and "code=" in redirect_url:
        redirect_url = "http://dummy?" + redirect_url
        parsed = urlparse(redirect_url)
    qs = parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    if not code:
        session.pop("pending_connect", None)
        return render_template_string(CONNECT_HTML, shared_keys=_list_shared_keys(), error="No 'code' in URL. Paste the full URL from the address bar after authorizing.")
    redirect_uri = pending.get("redirect_uri") or _oauth_redirect_uri()
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    try:
        data = ls.exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    except Exception as e:
        return render_template_string(CONNECT_HTML, shared_keys=_list_shared_keys(), error=f"Token exchange failed: {e}")
    conn_id = _create_connection(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        account_id=pending["account_id"],
        airtable_api_key=pending.get("airtable_api_key") or "",
        airtable_base_id=pending["airtable_base_id"],
        airtable_table_name=pending["airtable_table_name"],
    )
    if pending.get("shared_key_id") and pending.get("shared_key_password"):
        _unlock_shared_key(pending["shared_key_id"], pending["shared_key_password"], conn_id)
    create_after = pending.get("create_shared_key_after_connect")
    if create_after and create_after.get("label") and create_after.get("password") and create_after.get("api_key"):
        sk_id = _create_shared_key(create_after["label"], create_after["password"], create_after["api_key"])
        _unlock_shared_key(sk_id, create_after["password"], conn_id)
    session.pop("pending_connect", None)
    return redirect(url_for("connect_success", key=conn_id))


@app.route("/connect/callback")
def connect_callback():
    """Used when BACKEND_PUBLIC_URL is HTTPS (direct redirect from Lightspeed)."""
    state = request.args.get("state") or ""
    code = request.args.get("code") or ""
    pending = session.get("pending_connect") or {}
    if not code or pending.get("state") != state:
        return render_template_string(
            CONNECT_HTML,
            shared_keys=_list_shared_keys(),
            error="Invalid or expired link. Please start again from /connect.",
        ), 400
    redirect_uri = _oauth_redirect_uri()
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    try:
        data = ls.exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    except Exception as e:
        session.pop("pending_connect", None)
        return render_template_string(CONNECT_HTML, shared_keys=_list_shared_keys(), error=f"Token exchange failed: {e}"), 200
    conn_id = _create_connection(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        account_id=pending["account_id"],
        airtable_api_key=pending.get("airtable_api_key") or "",
        airtable_base_id=pending["airtable_base_id"],
        airtable_table_name=pending["airtable_table_name"],
    )
    if pending.get("shared_key_id") and pending.get("shared_key_password"):
        _unlock_shared_key(pending["shared_key_id"], pending["shared_key_password"], conn_id)
    create_after = pending.get("create_shared_key_after_connect")
    if create_after and create_after.get("label") and create_after.get("password") and create_after.get("api_key"):
        sk_id = _create_shared_key(create_after["label"], create_after["password"], create_after["api_key"])
        _unlock_shared_key(sk_id, create_after["password"], conn_id)
    session.pop("pending_connect", None)
    return redirect(url_for("connect_success", key=conn_id))


@app.route("/connect/success")
def connect_success():
    key = request.args.get("key") or ""
    if not key or not _get_connection(key):
        return "Invalid or expired connection key.", 404
    return render_template_string(SUCCESS_HTML, connection_key=key)


# ----- Shared store keys (one person uploads key + password; others unlock with password) -----


@app.route("/shared-keys/create", methods=["GET", "POST"])
def shared_keys_create():
    if request.method == "GET":
        return render_template_string(SHARED_KEY_CREATE_HTML)
    label = (request.form.get("label") or "").strip()
    password = request.form.get("password") or ""
    api_key = (request.form.get("api_key") or "").strip()
    if not label or not password or not api_key:
        return render_template_string(
            SHARED_KEY_CREATE_HTML,
            error="Please fill in screen name, password, and Airtable API key.",
        )
    try:
        _create_shared_key(label, password, api_key)
        return render_template_string(SHARED_KEY_CREATE_HTML, saved=True)
    except Exception as e:
        return render_template_string(SHARED_KEY_CREATE_HTML, error=str(e))


@app.route("/api/shared-keys", methods=["GET"])
def api_shared_keys_list():
    """List available shared keys (id and label only)."""
    keys = _list_shared_keys()
    return jsonify({"shared_keys": keys})


@app.route("/api/shared-keys", methods=["POST"])
def api_shared_keys_create():
    """Create a shared key (JSON: label, password, api_key)."""
    data = request.get_json() or {}
    label = (data.get("label") or "").strip()
    password = data.get("password") or ""
    api_key = (data.get("api_key") or "").strip()
    if not label or not password or not api_key:
        return jsonify({"success": False, "error": "Missing label, password, or api_key"}), 200
    try:
        sk_id = _create_shared_key(label, password, api_key)
        return jsonify({"success": True, "id": sk_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 200


@app.route("/api/shared-keys/<shared_key_id>/unlock", methods=["POST"])
def api_shared_keys_unlock(shared_key_id):
    """Unlock a shared key for a connection (JSON: password, connection_id)."""
    data = request.get_json() or {}
    password = data.get("password") or ""
    connection_id = (data.get("connection_id") or "").strip()
    if not password or not connection_id:
        return jsonify({"success": False, "error": "Missing password or connection_id"}), 200
    if not _get_connection(connection_id):
        return jsonify({"success": False, "error": "Connection not found"}), 200
    if _unlock_shared_key(shared_key_id, password, connection_id):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Wrong password"}), 200


@app.route("/api/connection-info", methods=["GET"])
def api_connection_info():
    """Return connection info for the extension (e.g. Airtable base URL). Key in query: key=connection_id."""
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "Missing key"}), 400
    row = _get_connection(key)
    if not row:
        return jsonify({"error": "Connection not found"}), 404
    base_id = (row["airtable_base_id"] or "").strip()
    airtable_base_url = f"https://airtable.com/{base_id}" if base_id else ""
    return jsonify({"connection_id": key, "airtable_base_url": airtable_base_url})


@app.route("/api/connection/update-base", methods=["POST"])
def api_connection_update_base():
    """Update the Airtable base for a connection. JSON: connection_id, airtable_base_url."""
    data = request.get_json() or {}
    connection_id = (data.get("connection_id") or "").strip()
    airtable_base_url = (data.get("airtable_base_url") or "").strip()
    if not connection_id or not airtable_base_url:
        return jsonify({"success": False, "error": "Missing connection_id or airtable_base_url"}), 200
    if not _get_connection(connection_id):
        return jsonify({"success": False, "error": "Connection not found"}), 200
    if _update_connection_base(connection_id, airtable_base_url):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid Airtable base URL"}), 200


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    key = (request.args.get("key") or request.form.get("key") or "").strip()
    if not key:
        return "Missing key. Open /settings?key=YOUR_CONNECTION_KEY", 400
    if not _get_connection(key):
        return "Invalid or expired connection key.", 404
    available = ls.AVAILABLE_FIELDS
    selected_ids = set(_get_selected_fields(key))
    if request.method == "POST":
        chosen = request.form.getlist("field")
        valid = {f["id"] for f in available}
        filtered = [x for x in chosen if x in valid]
        if not filtered:
            return render_template_string(
                SETTINGS_HTML,
                key=key,
                available_fields=available,
                selected_ids=selected_ids,
                error="Select at least one field.",
            )
        _update_connection_fields(key, filtered)
        airtable_key_input = (request.form.get("airtable_api_key") or "").strip()
        if airtable_key_input:
            _update_connection_airtable_key(key, airtable_key_input)
        selected_ids = set(filtered)
        return render_template_string(
            SETTINGS_HTML,
            key=key,
            available_fields=available,
            selected_ids=selected_ids,
            saved=True,
        )
    return render_template_string(
        SETTINGS_HTML,
        key=key,
        available_fields=available,
        selected_ids=selected_ids,
    )


def main():
    load_dotenv()
    _init_db()
    port = int(os.environ.get("PORT", 5050))
    # Bind to 0.0.0.0 when PORT is set (e.g. Railway, Render) so the server accepts external requests
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    base = os.environ.get("BACKEND_PUBLIC_URL", "").strip().rstrip("/") or f"http://{host}:{port}"
    print(f"Backend: {base}", file=sys.stderr)
    print("  GET  /connect = connect your Lightspeed + Airtable (once per user)", file=sys.stderr)
    print("  POST /api/run (connection_id, category_id) = run export (extension)", file=sys.stderr)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
