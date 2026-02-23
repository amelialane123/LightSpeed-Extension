#!/usr/bin/env python3
"""
Lightspeed → Airtable export backend (multi-tenant). Used by the Chrome extension.
Each user connects their own Lightspeed + Airtable once; tokens are stored and
refreshed automatically per connection.
Run: python export_backend.py
"""

from __future__ import annotations

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
except ImportError:
    print("Install Flask: pip install flask", file=sys.stderr)
    sys.exit(1)

import lightspeed_export as ls

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
SCRIPT_DIR = Path(__file__).resolve().parent
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
    except Exception:
        pass  # use existing tokens; export may still work
    return access_token, refresh_token


@app.after_request
def _cors(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ----- API (for extension) -----

@app.route("/api/run", methods=["OPTIONS"])
def api_run_options():
    return "", 204


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json() or {}
    connection_id = (data.get("connection_id") or "").strip()
    category_id = (data.get("category_id") or "").strip() or "ALL"
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
    # One server API key; per-connection base/table
    airtable_key = (row["airtable_api_key"] or "").strip() or os.environ.get("AIRTABLE_API_KEY", "")
    if not airtable_key:
        return jsonify({"success": False, "error": "Server missing AIRTABLE_API_KEY. Set it in .env (one key for all users)."}), 200
    selected = _get_selected_fields(connection_id)
    env = {
        **os.environ,
        "LIGHTSPEED_ACCESS_TOKEN": access_token,
        "LIGHTSPEED_REFRESH_TOKEN": refresh_token,
        "LIGHTSPEED_ACCOUNT_ID": row["account_id"],
        "AIRTABLE_API_KEY": airtable_key,
        "AIRTABLE_BASE_ID": row["airtable_base_id"],
        "AIRTABLE_TABLE_NAME": row["airtable_table_name"],
        "AIRTABLE_FIELDS": ",".join(selected),
        "FROM_EXPORT_BACKEND": "1",  # script skips opening browser; extension opens the tab
    }
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
    body { font-family: system-ui, sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }
    h1 { font-size: 1.25rem; }
    .error { color: #c00; margin: 0.5rem 0; }
    label { display: block; margin-top: 0.75rem; }
    input { width: 100%; padding: 0.5rem; margin: 0.25rem 0; box-sizing: border-box; }
    button, .btn { display: inline-block; padding: 0.5rem 1rem; background: #0a0; color: #fff; border: none; border-radius: 4px; cursor: pointer; margin-top: 0.75rem; }
    button:hover, .btn:hover { background: #080; }
    .muted { color: #666; font-size: 0.9rem; }
  </style>
</head>
<body>
  <h1>Connect your Lightspeed & Airtable</h1>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <p>Enter your details below, then continue to sign in with Lightspeed. You only need to do this once. Exports will create new tables in the Airtable you link below.</p>
  <form method="post" action="/connect/start" id="f">
    <label>Lightspeed Account ID <span class="muted">(find in your Lightspeed URL or settings)</span></label>
    <input type="text" name="account_id" placeholder="e.g. 12345" required>
    <label>Link to your Airtable</label>
    <p class="muted">Open the Airtable where you want exports to go, then copy the URL from your browser's address bar and paste it below.</p>
    <input type="text" name="airtable_base_url" placeholder="Paste the link when your Airtable is open in your browser" required>
    <button type="submit">Continue to Lightspeed login</button>
  </form>
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
    .key { font-family: monospace; background: #f0f0f0; padding: 0.75rem; word-break: break-all; margin: 1rem 0; }
    button { padding: 0.5rem 1rem; background: #06c; color: #fff; border: none; border-radius: 4px; cursor: pointer; }
    button:hover { background: #05a; }
    .muted { color: #666; font-size: 0.9rem; margin-top: 1rem; }
  </style>
</head>
<body>
  <h1>You're connected</h1>
  <p>Copy this connection key and paste it into the extension options (right‑click the extension icon → Options):</p>
  <div class="key" id="key">{{ connection_key }}</div>
  <button type="button" id="copy">Copy key</button>
  <p class="muted"><a href="/settings?key={{ connection_key }}">Configure which fields to export</a></p>
  <p class="muted">Keep this key private. Anyone with it can export from your Lightspeed to your Airtable. You can reconnect anytime to create a new key.</p>
  <script>
    document.getElementById('copy').onclick = function() {
      navigator.clipboard.writeText(document.getElementById('key').textContent);
      this.textContent = 'Copied!';
    };
  </script>
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
  </style>
</head>
<body>
  <h1>Choose export fields</h1>
  <p>Select which fields to include when exporting to Airtable. These settings are saved for this connection.</p>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  {% if saved %}<p class="saved">Settings saved.</p>{% endif %}
  <form method="post" action="/settings">
    <input type="hidden" name="key" value="{{ key }}">
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
    return render_template_string(CONNECT_HTML)


@app.route("/connect/start", methods=["POST"])
def connect_start():
    account_id = (request.form.get("account_id") or "").strip()
    airtable_input = (
        (request.form.get("airtable_base_url") or request.form.get("airtable_base_id") or "").strip()
    )
    airtable_base_id = _extract_airtable_base_id(airtable_input)
    airtable_table_name = (request.form.get("airtable_table_name") or "").strip() or "Items"
    if not account_id or not airtable_base_id:
        return render_template_string(
            CONNECT_HTML,
            error="Please fill in Account ID and paste the link to your Airtable.",
        )
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    if not client_id or not client_secret:
        return render_template_string(CONNECT_HTML, error="Server missing Lightspeed client credentials.")
    redirect_uri = _oauth_redirect_uri()
    state = secrets.token_urlsafe(24)
    session["pending_connect"] = {
        "state": state,
        "redirect_uri": redirect_uri,
        "account_id": account_id,
        "airtable_base_id": airtable_base_id,
        "airtable_table_name": airtable_table_name,
    }
    return redirect(url_for("connect_paste"))


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
        return render_template_string(CONNECT_HTML, error="No 'code' in URL. Paste the full URL from the address bar after authorizing.")
    redirect_uri = pending.get("redirect_uri") or _oauth_redirect_uri()
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    try:
        data = ls.exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    except Exception as e:
        return render_template_string(CONNECT_HTML, error=f"Token exchange failed: {e}")
    conn_id = _create_connection(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        account_id=pending["account_id"],
        airtable_api_key=pending.get("airtable_api_key") or "",
        airtable_base_id=pending["airtable_base_id"],
        airtable_table_name=pending["airtable_table_name"],
    )
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
            error="Invalid or expired link. Please start again from /connect.",
        ), 400
    redirect_uri = _oauth_redirect_uri()
    client_id = ls.env("LIGHTSPEED_CLIENT_ID")
    client_secret = ls.env("LIGHTSPEED_CLIENT_SECRET")
    try:
        data = ls.exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    except Exception as e:
        session.pop("pending_connect", None)
        return render_template_string(CONNECT_HTML, error=f"Token exchange failed: {e}"), 200
    conn_id = _create_connection(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        account_id=pending["account_id"],
        airtable_api_key=pending.get("airtable_api_key") or "",
        airtable_base_id=pending["airtable_base_id"],
        airtable_table_name=pending["airtable_table_name"],
    )
    session.pop("pending_connect", None)
    return redirect(url_for("connect_success", key=conn_id))


@app.route("/connect/success")
def connect_success():
    key = request.args.get("key") or ""
    if not key or not _get_connection(key):
        return "Invalid or expired connection key.", 404
    return render_template_string(SUCCESS_HTML, connection_key=key)


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
