#!/usr/bin/env python3
"""
Export Lightspeed Retail R-Series items (name, cost, price, vendor, images) to
an Airtable-ready format (JSON and CSV). Handles pagination for large catalogs (18k+ items).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import http.server
import json
import os
import secrets
import socketserver
import sys
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

import requests
from dotenv import load_dotenv

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
BASE_URL = "https://api.lightspeedapp.com/API/V3/Account"
# Use legacy PHP OAuth endpoints to avoid MerchantOS redirect loop (api.lightspeed.app "too many redirects")
AUTHORIZE_URL = "https://cloud.lightspeedapp.com/oauth/authorize.php"
TOKEN_URL = "https://cloud.lightspeedapp.com/oauth/access_token.php"
REFRESH_URL = "https://cloud.lightspeedapp.com/auth/oauth/token"
LOCAL_CALLBACK_PORT = 8765
LOCAL_REDIRECT_URI = f"http://127.0.0.1:{LOCAL_CALLBACK_PORT}/callback"
LIMIT = 100  # API max per request


def _rate_delay_sec() -> float:
    """Delay between Lightspeed API requests. Set EXPORT_LIGHTSPEED_DELAY in env to override (e.g. 0.15 if you get 429)."""
    raw = env("EXPORT_LIGHTSPEED_DELAY", "")
    if raw:
        try:
            return max(0.05, float(raw))
        except ValueError:
            pass
    return 0.1


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for access_token and refresh_token."""
    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if not resp.ok:
        try:
            err_body = resp.json()
            msg = err_body.get("error_description") or err_body.get("message") or resp.text
        except Exception:
            msg = resp.text
        print(f"Token exchange failed ({resp.status_code}): {msg}", file=sys.stderr)
        resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data or "refresh_token" not in data:
        raise ValueError("Token response missing access_token or refresh_token")
    return data


def _run_oauth_login_with_local_server(
    client_id: str, client_secret: str, state: str
) -> tuple[str, str]:
    """Use a local HTTP server to capture the OAuth callback automatically."""
    captured: dict[str, str | None] = {"code": None}

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/callback":
                qs = parse_qs(parsed.query)
                captured["code"] = (qs.get("code") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = b"<h1>Success!</h1><p>You can close this tab and return to the terminal.</p>"
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            pass

    print(
        f"Using local callback. If login fails, add this URL to your Lightspeed OAuth app:\n"
        f"  {LOCAL_REDIRECT_URI}",
        file=sys.stderr,
    )
    with socketserver.TCPServer(("127.0.0.1", LOCAL_CALLBACK_PORT), CallbackHandler) as server:
        server.timeout = 300  # 5 min to log in
        auth_url = (
            f"{AUTHORIZE_URL}?response_type=code&client_id={quote(client_id, safe='')}"
            f"&scope=employee:all&state={quote(state, safe='')}"
            f"&redirect_uri={quote(LOCAL_REDIRECT_URI, safe='')}"
        )
        print(
            "Opening browser for Lightspeed authorization...\n"
            "  (If you see 'MerchantOS' or 'Lightspeed ID' — that's the normal login; use your\n"
            "   usual Lightspeed Retail / POS email and password.)",
            file=sys.stderr,
        )
        webbrowser.open(auth_url)
        print("Waiting for you to sign in and authorize in the browser...", file=sys.stderr)
        while captured.get("code") is None:
            server.handle_request()

    code = captured.get("code")
    if not code:
        raise ValueError("No authorization code received. Try again.")
    data = exchange_code_for_tokens(code, client_id, client_secret, LOCAL_REDIRECT_URI)
    return data["access_token"], data["refresh_token"]


def _run_oauth_login_paste_url(
    client_id: str, client_secret: str, redirect_uri: str, state: str
) -> tuple[str, str]:
    """Open browser, then ask user to paste the redirect URL (for non-local redirect URIs)."""
    auth_url = (
        f"{AUTHORIZE_URL}?response_type=code&client_id={quote(client_id, safe='')}"
        f"&scope=employee:all&state={quote(state, safe='')}&redirect_uri={quote(redirect_uri, safe='')}"
    )
    print("Opening browser for Lightspeed authorization...", file=sys.stderr)
    webbrowser.open(auth_url)
    print(
        "After you authorize, copy the FULL URL from your browser's address bar\n"
        "(the page may be blank — the URL still contains the code) and paste it below.",
        file=sys.stderr,
    )
    raw = input("Paste redirect URL here: ").strip()
    parsed = urlparse(raw)
    if not parsed.query and "code=" in raw:
        raw = "http://dummy?" + raw
        parsed = urlparse(raw)
    if not parsed.query:
        raise ValueError("No query string in URL. Paste the full redirect URL including ?code=...")
    qs = parse_qs(parsed.query)
    code = (qs.get("code") or [None])[0]
    if not code:
        raise ValueError("No 'code' in URL. Paste the full redirect URL after authorizing.")
    data = exchange_code_for_tokens(code, client_id, client_secret, redirect_uri)
    return data["access_token"], data["refresh_token"]


def run_oauth_login(client_id: str, client_secret: str, redirect_uri: str) -> tuple[str, str]:
    """
    Open browser for user to authorize. If redirect_uri is the local callback,
    capture the code automatically; otherwise ask user to paste the redirect URL.
    Returns (access_token, refresh_token).
    """
    normalized = redirect_uri.strip().rstrip("/").lower()
    local_ok = normalized in (
        LOCAL_REDIRECT_URI.lower(),
        f"http://localhost:{LOCAL_CALLBACK_PORT}/callback",
    )
    state = secrets.token_urlsafe(16)

    if local_ok:
        return _run_oauth_login_with_local_server(client_id, client_secret, state)
    return _run_oauth_login_paste_url(client_id, client_secret, redirect_uri, state)


def update_env_tokens(access_token: str, refresh_token: str) -> None:
    """Update or append LIGHTSPEED_ACCESS_TOKEN and LIGHTSPEED_REFRESH_TOKEN in .env."""
    env_path = Path.cwd() / ".env"
    lines: list[str] = []
    updated = {"LIGHTSPEED_ACCESS_TOKEN": access_token, "LIGHTSPEED_REFRESH_TOKEN": refresh_token}
    done = set()
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            for key in updated:
                if line.strip().startswith(f"{key}="):
                    lines.append(f"{key}={updated[key]}")
                    done.add(key)
                    break
            else:
                lines.append(line)
    for key in updated:
        if key not in done:
            lines.append(f"{key}={updated[key]}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Saved access token and refresh token to .env", file=sys.stderr)


def refresh_oauth_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> dict:
    """Exchange a refresh token for a new access token (and new refresh token)."""
    resp = requests.post(
        REFRESH_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if not resp.ok:
        try:
            err_body = resp.json()
            msg = err_body.get("error_description") or err_body.get("message") or resp.text
        except Exception:
            msg = resp.text
        print(f"Token refresh failed ({resp.status_code}): {msg}", file=sys.stderr)
        resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError("Refresh response missing access_token")
    return data


class SessionWithRefresh:
    """
    Session that uses an access token and refreshes automatically on 401.
    Holds refresh_token, client_id, client_secret; after each refresh, the new
    refresh_token is stored for the next refresh (old one is revoked once used).
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
    ) -> None:
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._update_auth_header()

    def _update_auth_header(self) -> None:
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"

    def _refresh(self) -> None:
        try:
            data = refresh_oauth_token(
                self._refresh_token,
                self._client_id,
                self._client_secret,
            )
        except requests.exceptions.HTTPError:
            print(
                "Lightspeed sign-in has expired or was revoked. Please reconnect: "
                "open the extension options, click Reconnect, and complete the connection flow again.",
                file=sys.stderr,
            )
            sys.exit(1)
        self._access_token = data["access_token"]
        if data.get("refresh_token"):
            self._refresh_token = data["refresh_token"]
        self._update_auth_header()
        print("  Refreshed Lightspeed access token.", file=sys.stderr)

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        params = params or {}
        resp = self._session.get(url, params=params)
        if resp.status_code == 401:
            self._refresh()
            resp = self._session.get(url, params=params)
        return resp


def get_session(access_token: str) -> requests.Session:
    """Plain session with no refresh (for when refresh credentials are not provided)."""
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {access_token}"
    s.headers["Accept"] = "application/json"
    return s


def get_next_url(data: dict) -> str:
    attrs = data.get("@attributes") or data.get("attributes") or {}
    return (attrs.get("next") or "").strip()


def fetch_all_paginated(
    session: requests.Session | SessionWithRefresh,
    account_id: str,
    resource: str,
    load_relations: list[str] | None = None,
    sort: str | None = None,
    extra_params: dict | None = None,
) -> list[dict]:
    """Fetch all records for a resource using cursor-based pagination."""
    url = f"{BASE_URL}/{account_id}/{resource}.json"
    sort_field = sort or ("itemID" if resource == "Item" else "vendorID")
    params = {"limit": LIMIT, "sort": sort_field}
    if load_relations:
        params["load_relations"] = json.dumps(load_relations)
    if extra_params:
        params.update(extra_params)

    all_records: list[dict] = []
    page = 0

    while True:
        page += 1
        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        # Normalize: API returns either {"Item": {...}} (single) or {"Item": [...]}
        key = resource  # e.g. "Item" or "Vendor"
        raw = data.get(key)
        if raw is None:
            break
        if isinstance(raw, dict):
            raw = [raw]
        all_records.extend(raw)

        next_url = get_next_url(data)
        if not next_url:
            break
        url = next_url
        params = {}  # next URL has everything

        time.sleep(_rate_delay_sec())
        if page % 50 == 0:
            print(f"  ... fetched {len(all_records)} {resource} records so far", file=sys.stderr)

    return all_records


def build_vendor_map(
    session: requests.Session | SessionWithRefresh, account_id: str
) -> dict[str, str]:
    """Fetch all vendors and return mapping vendorID -> name."""
    vendors = fetch_all_paginated(session, account_id, "Vendor", sort="vendorID")
    return {str(v.get("vendorID", "")): (v.get("name") or "").strip() for v in vendors}


def get_default_price(item: dict) -> str:
    """Extract default retail price from Item.Prices.ItemPrice (useType 'Default')."""
    prices = item.get("Prices") or {}
    item_prices = prices.get("ItemPrice")
    if not item_prices:
        return ""
    if isinstance(item_prices, dict):
        item_prices = [item_prices]
    for p in item_prices:
        if (p.get("useType") or "").strip() == "Default":
            return (p.get("amount") or "").strip()
    return (item_prices[0].get("amount") or "").strip() if item_prices else ""


def get_msrp(item: dict) -> str:
    """Extract MSRP from Item.Prices.ItemPrice (useType 'MSRP')."""
    prices = item.get("Prices") or {}
    item_prices = prices.get("ItemPrice")
    if not item_prices:
        return ""
    if isinstance(item_prices, dict):
        item_prices = [item_prices]
    for p in item_prices:
        if (p.get("useType") or "").strip() == "MSRP":
            return (p.get("amount") or "").strip()
    return ""


def build_category_path_map(
    session: requests.Session | SessionWithRefresh, account_id: str
) -> dict[str, list[str]]:
    """Fetch categories and return categoryID -> [level0, level1, ...] from fullPathName split by /."""
    categories = fetch_all_paginated(session, account_id, "Category", sort="categoryID")
    out: dict[str, list[str]] = {}
    for c in categories:
        cid = str(c.get("categoryID", ""))
        path = (c.get("fullPathName") or c.get("name") or "").strip()
        parts = [p.strip() for p in path.split("/") if p.strip()]
        out[cid] = parts
    return out


def build_manufacturer_map(
    session: requests.Session | SessionWithRefresh, account_id: str
) -> dict[str, str]:
    """Fetch manufacturers and return manufacturerID -> name (Brand)."""
    try:
        manufacturers = fetch_all_paginated(
            session, account_id, "Manufacturer", sort="manufacturerID"
        )
        return {str(m.get("manufacturerID", "")): (m.get("name") or "").strip() for m in manufacturers}
    except Exception:
        return {}


def build_department_map(
    session: requests.Session | SessionWithRefresh, account_id: str
) -> dict[str, str]:
    """Fetch departments and return departmentID -> name. Not available on all accounts."""
    try:
        departments = fetch_all_paginated(
            session, account_id, "Department", sort="departmentID"
        )
        return {str(d.get("departmentID", "")): (d.get("name") or "").strip() for d in departments}
    except Exception:
        return {}


def get_average_cost(item: dict) -> str:
    """Extract average cost from Item.ItemShops (shopID=0 summary record)."""
    shops = item.get("ItemShops") or {}
    shop_list = shops.get("ItemShop") if isinstance(shops, dict) else []
    if not shop_list:
        return ""
    if isinstance(shop_list, dict):
        shop_list = [shop_list]
    for s in shop_list:
        if str(s.get("shopID", "")) == "0":
            return (s.get("averageCost") or "").strip()
    return (shop_list[0].get("averageCost") or "").strip() if shop_list else ""


def get_item_note(item: dict) -> str:
    """Extract first note text from Item.Note."""
    notes = item.get("Note") or {}
    note_list = notes.get("Note") if isinstance(notes, dict) else []
    if isinstance(note_list, dict):
        note_list = [note_list] if note_list else []
    elif not isinstance(note_list, list):
        note_list = []
    for n in note_list:
        if isinstance(n, dict) and (n.get("note") or "").strip():
            return (n.get("note") or "").strip()
    return ""


def get_item_ecommerce(item: dict) -> dict:
    """Extract weight, length, width, height from Item.ItemECommerce (relation)."""
    ecom = item.get("ItemECommerce") or item.get("itemECommerce") or {}
    if isinstance(ecom, dict) and ecom.get("itemECommerceID") is not None:
        return ecom
    # Sometimes returned as list/wrapper
    ecom_list = ecom.get("ItemECommerce") or ecom.get("itemECommerce") if isinstance(ecom, dict) else []
    if isinstance(ecom_list, dict):
        ecom_list = [ecom_list]
    return (ecom_list[0] or {}) if ecom_list else {}


def get_image_urls(item: dict) -> list[str]:
    """Extract image URLs from Item if Images relation was loaded."""
    urls: list[str] = []
    images = item.get("Image") or item.get("Images") or {}
    if isinstance(images, dict):
        img_list = images.get("Image") or images.get("ImageMatrix") or []
        if isinstance(img_list, dict):
            img_list = [img_list]
    elif isinstance(images, list):
        img_list = images
    else:
        return urls
    for img in img_list:
        if isinstance(img, dict):
            # baseImageURL + publicID is common for Lightspeed/Cloudinary
            base = (img.get("baseImageURL") or "").strip()
            pid = (img.get("publicID") or "").strip()
            if base and pid:
                urls.append(f"{base.rstrip('/')}/{pid}")
            elif img.get("url"):
                urls.append(img.get("url", "").strip())
    return urls


def item_to_row(
    item: dict,
    vendor_map: dict[str, str],
    include_images: bool,
    category_path_map: dict[str, list[str]] | None = None,
    manufacturer_map: dict[str, str] | None = None,
    department_map: dict[str, str] | None = None,
) -> dict:
    """Convert one Lightspeed Item to a flat row for Airtable."""
    item_id = item.get("itemID", "")
    vendor_id = str(item.get("defaultVendorID") or "")
    vendor_name = (vendor_map or {}).get(vendor_id, "")
    cat_id = str(item.get("categoryID") or "")
    path_parts = (category_path_map or {}).get(cat_id, [])
    # Category = first level, Subcategory 1..9 = next levels
    category = path_parts[0] if len(path_parts) >= 1 else ""
    subcategories = path_parts[1:10]  # up to 9 subcategories
    while len(subcategories) < 9:
        subcategories.append("")

    manufacturer_id = str(item.get("manufacturerID") or "")
    department_id = str(item.get("departmentID") or "")
    ecom = get_item_ecommerce(item)

    row = {
        "itemID": str(item_id),
        "name": (item.get("description") or "").strip(),
        "cost": (item.get("defaultCost") or "").strip(),
        "price": get_default_price(item),
        "msrp": get_msrp(item),
        "vendor_id": vendor_id,
        "vendor_name": vendor_name,
        "systemSku": (item.get("systemSku") or "").strip(),
        "customSku": (item.get("customSku") or "").strip(),
        "upc": (item.get("upc") or "").strip(),
        "ean": (item.get("ean") or "").strip(),
        "manufacturerSku": (item.get("manufacturerSku") or "").strip(),
        "year": (item.get("modelYear") or "").strip() if item.get("modelYear") not in (None, "", "0") else "",
        "tax": "Yes" if item.get("tax") in (True, "true", "1") else "No",
        "brand": (manufacturer_map or {}).get(manufacturer_id, ""),
        "department": (department_map or {}).get(department_id, ""),
        "averageCost": get_average_cost(item),
        "note": get_item_note(item),
        "category": category,
        "subcategory_1": subcategories[0],
        "subcategory_2": subcategories[1],
        "subcategory_3": subcategories[2],
        "subcategory_4": subcategories[3],
        "subcategory_5": subcategories[4],
        "subcategory_6": subcategories[5],
        "subcategory_7": subcategories[6],
        "subcategory_8": subcategories[7],
        "subcategory_9": subcategories[8],
        "weight": (ecom.get("weight") or "").strip() if isinstance(ecom, dict) else "",
        "length": (ecom.get("length") or "").strip() if isinstance(ecom, dict) else "",
        "width": (ecom.get("width") or "").strip() if isinstance(ecom, dict) else "",
        "height": (ecom.get("height") or "").strip() if isinstance(ecom, dict) else "",
    }
    if include_images:
        urls = get_image_urls(item)
        row["image_urls"] = " | ".join(urls) if urls else ""
        row["image_count"] = len(urls)
    return row


def get_category_name(
    session: requests.Session | SessionWithRefresh,
    account_id: str,
    category_id: str,
) -> str:
    """Return display name for a category (fullPathName or name)."""
    categories = fetch_all_paginated(session, account_id, "Category", sort="categoryID")
    for c in categories:
        if str(c.get("categoryID", "")) == str(category_id):
            return (c.get("fullPathName") or c.get("name") or "").strip() or f"Category {category_id}"
    return f"Category {category_id}"


def _relations_for_field_ids(field_ids: list[str]) -> list[str]:
    """Return which Item relations we need to load for the selected fields."""
    rels: list[str] = []
    if "image" in field_ids:
        rels.append("Images")
    if "averageCost" in field_ids:
        rels.append("ItemShops")
    if "note" in field_ids:
        rels.append("Note")
    if any(x in field_ids for x in ("weight", "length", "width", "height")):
        rels.append("ItemECommerce")
    return rels


def export_items(
    session: requests.Session | SessionWithRefresh,
    account_id: str,
    load_relations: list[str] | None,
    include_images: bool,
    category_id: str | None = None,
    field_ids: list[str] | None = None,
) -> list[dict]:
    """Fetch all items and vendors, return list of Airtable-ready rows.
    When field_ids is set, only fetches relations and lookup data needed for those fields.
    """
    ids = field_ids or _field_ids_from_env()
    needed_relations = _relations_for_field_ids(ids)
    # Always include Images if caller asked for include_images (e.g. default export)
    if include_images and "Images" not in needed_relations:
        needed_relations.append("Images")

    # Only fetch lookup tables that selected fields need
    need_vendor = "vendor_name" in ids
    need_category = "category" in ids or any(f"subcategory_{i}" in ids for i in range(1, 10))
    need_manufacturer = "brand" in ids
    need_department = "department" in ids

    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        if need_vendor:
            tasks.append(("vendor", executor.submit(build_vendor_map, session, account_id)))
        if need_category:
            tasks.append(("category", executor.submit(build_category_path_map, session, account_id)))
        if need_manufacturer:
            tasks.append(("manufacturer", executor.submit(build_manufacturer_map, session, account_id)))
        if need_department:
            tasks.append(("department", executor.submit(build_department_map, session, account_id)))

    vendor_map: dict[str, str] = {}
    category_path_map: dict[str, list[str]] = {}
    manufacturer_map: dict[str, str] = {}
    department_map: dict[str, str] = {}
    for name, fut in tasks:
        result = fut.result()
        if name == "vendor":
            vendor_map = result
        elif name == "category":
            category_path_map = result
        elif name == "manufacturer":
            manufacturer_map = result
        elif name == "department":
            department_map = result
    if tasks:
        parts = []
        if need_vendor:
            parts.append(f"{len(vendor_map)} vendors")
        if need_category:
            parts.append(f"{len(category_path_map)} categories")
        if need_manufacturer:
            parts.append(f"{len(manufacturer_map)} manufacturers")
        if need_department:
            parts.append(f"{len(department_map)} departments")
        print(f"  Loaded {', '.join(parts)} (for selected fields).", file=sys.stderr)

    relations = list(load_relations or [])
    for rel in needed_relations:
        if rel not in relations:
            relations.append(rel)

    extra = {"categoryID": category_id} if category_id else None
    if category_id:
        print(f"Filtering items by category ID: {category_id}", file=sys.stderr)
    print("Fetching items (paginated)...", file=sys.stderr)
    try:
        items = fetch_all_paginated(
            session,
            account_id,
            "Item",
            load_relations=relations,
            sort="itemID",
            extra_params=extra,
        )
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 400:
            print(
                "  API returned 400 with full relations; retrying with Images only (some fields may be empty).",
                file=sys.stderr,
            )
            relations = ["Images"]
            items = fetch_all_paginated(
                session,
                account_id,
                "Item",
                load_relations=relations,
                sort="itemID",
                extra_params=extra,
            )
        else:
            raise
    print(f"  Loaded {len(items)} items.", file=sys.stderr)

    rows = [
        item_to_row(
            item,
            vendor_map,
            include_images=include_images or "Images" in relations,
            category_path_map=category_path_map,
            manufacturer_map=manufacturer_map,
            department_map=department_map,
        )
        for item in items
    ]
    return rows


def write_json(path: Path, rows: list[dict], airtable_style: bool) -> None:
    if airtable_style:
        # Airtable create records format: { "records": [ { "fields": { ... } }, ... ] }
        records = [{"fields": {k: v for k, v in r.items() if v != ""}} for r in rows]
        payload = {"records": records}
    else:
        payload = rows
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)


def _to_number(s: str) -> float | None:
    """Parse string to float for Airtable Number/Currency fields; return None if invalid."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Available export fields: id, displayName, Airtable type, row key, and how to get value.
# Default fields (Name, Cost, Price, Vendor Name, Image) are used when AIRTABLE_FIELDS is not set.
DEFAULT_FIELD_IDS = ["name", "cost", "price", "vendor_name", "image"]

AVAILABLE_FIELDS = [
    {"id": "name", "displayName": "Name", "type": "singleLineText", "rowKey": "name"},
    {"id": "cost", "displayName": "Cost", "type": "number", "rowKey": "cost"},
    {"id": "price", "displayName": "Price", "type": "number", "rowKey": "price"},
    {"id": "vendor_name", "displayName": "Vendor Name", "type": "singleLineText", "rowKey": "vendor_name"},
    {"id": "image", "displayName": "Image", "type": "multipleAttachments", "rowKey": "image_urls"},
    {"id": "itemID", "displayName": "Item ID", "type": "singleLineText", "rowKey": "itemID"},
    {"id": "systemSku", "displayName": "System SKU", "type": "singleLineText", "rowKey": "systemSku"},
    {"id": "customSku", "displayName": "Custom SKU", "type": "singleLineText", "rowKey": "customSku"},
    {"id": "upc", "displayName": "UPC", "type": "singleLineText", "rowKey": "upc"},
    {"id": "ean", "displayName": "EAN", "type": "singleLineText", "rowKey": "ean"},
    {"id": "manufacturerSku", "displayName": "Manufacture SKU", "type": "singleLineText", "rowKey": "manufacturerSku"},
    {"id": "year", "displayName": "Year", "type": "singleLineText", "rowKey": "year"},
    {"id": "tax", "displayName": "Tax", "type": "singleLineText", "rowKey": "tax"},
    {"id": "brand", "displayName": "Brand", "type": "singleLineText", "rowKey": "brand"},
    {"id": "department", "displayName": "Department", "type": "singleLineText", "rowKey": "department"},
    {"id": "msrp", "displayName": "MSRP", "type": "number", "rowKey": "msrp"},
    {"id": "averageCost", "displayName": "Average Cost", "type": "number", "rowKey": "averageCost"},
    {"id": "note", "displayName": "Note", "type": "singleLineText", "rowKey": "note"},
    {"id": "category", "displayName": "Category", "type": "singleLineText", "rowKey": "category"},
    {"id": "subcategory_1", "displayName": "Subcategory 1", "type": "singleLineText", "rowKey": "subcategory_1"},
    {"id": "subcategory_2", "displayName": "Subcategory 2", "type": "singleLineText", "rowKey": "subcategory_2"},
    {"id": "subcategory_3", "displayName": "Subcategory 3", "type": "singleLineText", "rowKey": "subcategory_3"},
    {"id": "subcategory_4", "displayName": "Subcategory 4", "type": "singleLineText", "rowKey": "subcategory_4"},
    {"id": "subcategory_5", "displayName": "Subcategory 5", "type": "singleLineText", "rowKey": "subcategory_5"},
    {"id": "subcategory_6", "displayName": "Subcategory 6", "type": "singleLineText", "rowKey": "subcategory_6"},
    {"id": "subcategory_7", "displayName": "Subcategory 7", "type": "singleLineText", "rowKey": "subcategory_7"},
    {"id": "subcategory_8", "displayName": "Subcategory 8", "type": "singleLineText", "rowKey": "subcategory_8"},
    {"id": "subcategory_9", "displayName": "Subcategory 9", "type": "singleLineText", "rowKey": "subcategory_9"},
    {"id": "weight", "displayName": "Weight", "type": "number", "rowKey": "weight"},
    {"id": "length", "displayName": "Length", "type": "number", "rowKey": "length"},
    {"id": "width", "displayName": "Width", "type": "number", "rowKey": "width"},
    {"id": "height", "displayName": "Height", "type": "number", "rowKey": "height"},
]


def _field_ids_from_env() -> list[str]:
    """Parse AIRTABLE_FIELDS env (comma-separated ids). Empty or invalid = use defaults."""
    raw = env("AIRTABLE_FIELDS", "").strip()
    if not raw:
        return DEFAULT_FIELD_IDS
    ids = [x.strip().lower() for x in raw.split(",") if x.strip()]
    valid = {f["id"] for f in AVAILABLE_FIELDS}
    filtered = [i for i in ids if i in valid]
    return filtered if filtered else DEFAULT_FIELD_IDS


def _fields_for_ids(ids: list[str]) -> list[dict]:
    """Return AVAILABLE_FIELDS entries for given ids, preserving order."""
    by_id = {f["id"]: f for f in AVAILABLE_FIELDS}
    return [by_id[i] for i in ids if i in by_id]


def row_to_airtable_fields(row: dict, field_ids: list[str] | None = None) -> dict:
    """Build Airtable fields dict. Uses field_ids or AIRTABLE_FIELDS env; defaults to Name, Cost, Price, Vendor Name, Image."""
    ids = field_ids or _field_ids_from_env()
    fields_spec = _fields_for_ids(ids)
    out: dict = {}
    for f in fields_spec:
        key = f["rowKey"]
        val = row.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            if f["type"] == "number":
                num = _to_number(str(val or ""))
                if num is not None:
                    out[f["displayName"]] = num
            continue
        if f["type"] == "number":
            num = _to_number(str(val))
            if num is not None:
                out[f["displayName"]] = num
        elif f["type"] == "multipleAttachments":
            urls = [u.strip() for u in (str(val or "").split("|")) if u.strip()]
            if urls:
                out[f["displayName"]] = [{"url": u} for u in urls]
        else:
            out[f["displayName"]] = (str(val) or "").strip()
    return out


def _build_table_schema(field_ids: list[str] | None = None) -> list[dict]:
    """Build Airtable table fields schema for create API."""
    ids = field_ids or _field_ids_from_env()
    fields_spec = _fields_for_ids(ids)
    schema: list[dict] = []
    for f in fields_spec:
        if f["type"] == "number":
            schema.append({"name": f["displayName"], "type": "number", "options": {"precision": 0}})
        elif f["type"] == "multipleAttachments":
            schema.append({"name": f["displayName"], "type": "multipleAttachments"})
        else:
            schema.append({"name": f["displayName"], "type": "singleLineText"})
    return schema


AIRTABLE_API_BASE = "https://api.airtable.com/v0"
AIRTABLE_META_BASE = "https://api.airtable.com/v0/meta"
AIRTABLE_BATCH_SIZE = 10  # max records per create request


def _airtable_rate_delay() -> float:
    """Delay between Airtable batch requests. Airtable allows 5 req/sec; 0.18 is slightly faster, 0.2 is safe."""
    raw = env("EXPORT_AIRTABLE_DELAY", "")
    if raw:
        try:
            return max(0.15, float(raw))
        except ValueError:
            pass
    return 0.18


def _sanitize_table_name(name: str) -> str:
    """Make a string safe for Airtable table name (strip, replace invalid chars, limit length)."""
    s = (name or "").strip()
    for c in ["/", "\\", "?", "#"]:
        s = s.replace(c, " ")
    s = " ".join(s.split())[:100]
    return s or "Untitled"


def create_airtable_table(
    api_key: str, base_id: str, table_name: str, field_ids: list[str] | None = None
) -> str:
    """Create a new table in the base. Returns table id (tblxxx) for push."""
    url = f"{AIRTABLE_META_BASE}/bases/{base_id}/tables"
    name = _sanitize_table_name(table_name)
    if not name:
        name = "Exported items"
    # Avoid 422 "duplicate name" by making name unique (Airtable bases can't have two tables with same name)
    from datetime import datetime
    unique_name = f"{name} ({datetime.now().strftime('%Y-%m-%d %H.%M')})"
    schema = _build_table_schema(field_ids)
    payload = {"name": unique_name, "description": "Exported from Lightspeed", "fields": schema}
    resp = requests.post(
        url,
        json=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    if not resp.ok:
        body = (resp.text or "")[:600]
        try:
            err = resp.json()
            msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else str(err)
            print(f"  Airtable create table error ({resp.status_code}): {msg}", file=sys.stderr)
            print(f"  Response: {body}", file=sys.stderr)
        except Exception:
            print(f"  Airtable create table error ({resp.status_code}): {body}", file=sys.stderr)
        raise ValueError(f"Airtable 422 create table: {body}") from None
    data = resp.json()
    # Return table ID (tblxxx) for Records API; avoids 403 when pushing to a just-created table by name
    table_id = (data.get("id") or "").strip()
    created_name = (data.get("name") or payload["name"] or "").strip()
    print(f"  Created Airtable table: {created_name}", file=sys.stderr)
    return table_id if table_id else created_name


def push_to_airtable(
    rows: list[dict],
    api_key: str,
    base_id: str,
    table_name: str,
    field_ids: list[str] | None = None,
) -> None:
    """Create Airtable records in batches. Uses Images as Attachment field so photos display."""
    if not rows:
        print("No rows to push to Airtable.", file=sys.stderr)
        return
    ids = field_ids or _field_ids_from_env()
    total = 0
    for i in range(0, len(rows), AIRTABLE_BATCH_SIZE):
        batch = rows[i : i + AIRTABLE_BATCH_SIZE]
        records = [{"fields": row_to_airtable_fields(r, ids)} for r in batch]
        url = f"{AIRTABLE_API_BASE}/{base_id}/{quote(table_name, safe='')}"
        resp = requests.post(
            url,
            json={"records": records},
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code == 429:
            print("  Airtable rate limit; waiting 30s...", file=sys.stderr)
            time.sleep(30)
            resp = requests.post(
                url,
                json={"records": records},
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
        if not resp.ok:
            body = (resp.text or "")[:400]
            try:
                err = resp.json()
                msg = err.get("error", {}).get("message") if isinstance(err.get("error"), dict) else str(err)
                print(f"  Airtable error ({resp.status_code}): {msg}", file=sys.stderr)
            except Exception:
                print(f"  Airtable error ({resp.status_code}): {body}", file=sys.stderr)
            if resp.status_code == 403:
                print(f"  403 response body: {body}", file=sys.stderr)
                print(
                    "  Fix: airtable.com/create/tokens -> add data.records:read + data.records:write, "
                    "add this base (or its workspace). Copy the token again after saving and set AIRTABLE_API_KEY in .env, then restart the backend.",
                    file=sys.stderr,
                )
        resp.raise_for_status()
        total += len(batch)
        if total % 500 == 0 or total == len(rows):
            print(f"  Pushed {total}/{len(rows)} records to Airtable.", file=sys.stderr)
        time.sleep(_airtable_rate_delay())
    print(f"Pushed {total} records to Airtable.", file=sys.stderr)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Export Lightspeed R-Series items to Airtable-ready JSON/CSV."
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=Path("output"),
        help="Directory to write output files (default: output)",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=["json", "csv", "both"],
        default="both",
        help="Output format (default: both)",
    )
    parser.add_argument(
        "--airtable-json",
        action="store_true",
        help="Emit JSON in Airtable API format (records with 'fields').",
    )
    parser.add_argument(
        "--access-token",
        default=env("LIGHTSPEED_ACCESS_TOKEN"),
        help="Lightspeed access token (or set LIGHTSPEED_ACCESS_TOKEN)",
    )
    parser.add_argument(
        "--account-id",
        default=env("LIGHTSPEED_ACCOUNT_ID"),
        help="Lightspeed account ID (or set LIGHTSPEED_ACCOUNT_ID)",
    )
    parser.add_argument(
        "--refresh-token",
        default=env("LIGHTSPEED_REFRESH_TOKEN"),
        help="OAuth refresh token for auto-refresh (or set LIGHTSPEED_REFRESH_TOKEN)",
    )
    parser.add_argument(
        "--client-id",
        default=env("LIGHTSPEED_CLIENT_ID"),
        help="OAuth client ID (or set LIGHTSPEED_CLIENT_ID)",
    )
    parser.add_argument(
        "--client-secret",
        default=env("LIGHTSPEED_CLIENT_SECRET"),
        help="OAuth client secret (or set LIGHTSPEED_CLIENT_SECRET)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run OAuth login flow only (open browser, save tokens to .env), then exit.",
    )
    parser.add_argument(
        "--redirect-uri",
        default=env("LIGHTSPEED_REDIRECT_URI") or LOCAL_REDIRECT_URI,
        help="OAuth redirect URI. Use http://127.0.0.1:8765/callback for automatic capture "
        "(add that URL to your Lightspeed OAuth app).",
    )
    parser.add_argument(
        "--no-push-airtable",
        action="store_true",
        help="Skip auto-push to Airtable (by default pushes when AIRTABLE_API_KEY and "
        "AIRTABLE_BASE_ID are set). Images are uploaded as Attachment fields (actual photos).",
    )
    parser.add_argument(
        "--category-id",
        default=env("LIGHTSPEED_CATEGORY_ID"),
        help="Export only items in this category (set LIGHTSPEED_CATEGORY_ID in .env). "
        "Use --list-categories to see available category IDs.",
    )
    parser.add_argument(
        "--list-categories",
        action="store_true",
        help="List all item categories (ID and name) and exit. Use this to find --category-id.",
    )
    args = parser.parse_args()

    if not args.account_id and not args.login:
        print(
            "Error: Provide LIGHTSPEED_ACCOUNT_ID (env or --account-id).",
            file=sys.stderr,
        )
        sys.exit(1)

    has_refresh = args.refresh_token and args.client_id and args.client_secret

    if args.login:
        if not args.client_id or not args.client_secret:
            print(
                "Error: For --login set LIGHTSPEED_CLIENT_ID and LIGHTSPEED_CLIENT_SECRET in .env.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.redirect_uri:
            print(
                "Error: For --login set LIGHTSPEED_REDIRECT_URI in .env to match your OAuth app\n"
                "(e.g. https://oauth.pstmn.io/v1/callback for Postman).",
                file=sys.stderr,
            )
            sys.exit(1)
        access_token, refresh_token = run_oauth_login(
            args.client_id, args.client_secret, args.redirect_uri
        )
        update_env_tokens(access_token, refresh_token)
        print("Login complete. Run the script again (without --login) to export.", file=sys.stderr)
        return

    if not args.access_token and not has_refresh:
        if args.client_id and args.client_secret and args.redirect_uri:
            print("No refresh token found. Starting login flow...", file=sys.stderr)
            access_token, refresh_token = run_oauth_login(
                args.client_id, args.client_secret, args.redirect_uri
            )
            update_env_tokens(access_token, refresh_token)
            args.refresh_token = refresh_token
            args.access_token = access_token
            has_refresh = True
        else:
            print(
                "Error: Provide either LIGHTSPEED_ACCESS_TOKEN or (for auto-login) set\n"
                "LIGHTSPEED_CLIENT_ID, LIGHTSPEED_CLIENT_SECRET, and LIGHTSPEED_REDIRECT_URI in .env.",
                file=sys.stderr,
            )
            sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if has_refresh:
        access_token = args.access_token
        if not access_token:
            print("Getting initial access token via refresh...", file=sys.stderr)
            try:
                data = refresh_oauth_token(
                    args.refresh_token, args.client_id, args.client_secret
                )
                access_token = data["access_token"]
                if data.get("refresh_token"):
                    args.refresh_token = data["refresh_token"]
                    update_env_tokens(access_token, args.refresh_token)
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 400:
                    try:
                        err = e.response.json()
                        if err.get("error") == "invalid_grant":
                            print("Refresh token invalid. Starting login flow...", file=sys.stderr)
                            if not args.redirect_uri:
                                print(
                                    "Error: Set LIGHTSPEED_REDIRECT_URI in .env to re-login.",
                                    file=sys.stderr,
                                )
                                raise
                            access_token, args.refresh_token = run_oauth_login(
                                args.client_id, args.client_secret, args.redirect_uri
                            )
                            update_env_tokens(access_token, args.refresh_token)
                        else:
                            raise
                    except (ValueError, KeyError):
                        raise
                else:
                    raise
        session = SessionWithRefresh(
            access_token,
            args.refresh_token,
            args.client_id,
            args.client_secret,
        )
    else:
        session = get_session(args.access_token)

    if args.list_categories:
        categories = fetch_all_paginated(session, args.account_id, "Category", sort="categoryID")
        print("Item categories (use --category-id ID to filter):", file=sys.stderr)
        for c in categories:
            cid = c.get("categoryID", "")
            name = (c.get("name") or "").strip()
            path = (c.get("fullPathName") or name).strip()
            print(f"  {cid:>6}  {path}", file=sys.stderr)
        return

    # Only fetch relations and lookup data needed for the selected export fields
    category_id = args.category_id.strip() if args.category_id else None
    field_ids = _field_ids_from_env()
    rows = export_items(
        session,
        args.account_id,
        load_relations=[],
        include_images=("image" in field_ids),
        category_id=category_id,
        field_ids=field_ids,
    )

    base = args.output_dir / "lightspeed_items"
    if args.format in ("json", "both"):
        path = base.with_suffix(".json")
        write_json(path, rows, airtable_style=args.airtable_json)
        print(f"Wrote {path}", file=sys.stderr)
    if args.format in ("csv", "both"):
        path = base.with_suffix(".csv")
        write_csv(path, rows)
        print(f"Wrote {path}", file=sys.stderr)

    if not args.no_push_airtable:
        api_key = env("AIRTABLE_API_KEY") or env("AIRTABLE_TOKEN")
        base_id = env("AIRTABLE_BASE_ID")
        create_new = (env("AIRTABLE_CREATE_NEW_TABLE") or "").strip().lower() in ("1", "true", "yes")
        if api_key and base_id:
            field_ids = _field_ids_from_env()
            if create_new:
                if category_id:
                    display_name = get_category_name(session, args.account_id, category_id)
                else:
                    display_name = "All categories"
                display_name = _sanitize_table_name(display_name)
                print(f"Creating new Airtable table: {display_name}", file=sys.stderr)
                table_name = create_airtable_table(api_key, base_id, display_name, field_ids)  # returns table id (tblxxx)
                print(f"AIRTABLE_TABLE_ID={table_name}", file=sys.stderr)  # for backend to open that table
            else:
                table_name = env("AIRTABLE_TABLE_NAME") or "Items"
            print("Pushing to Airtable (images as photos)...", file=sys.stderr)
            push_to_airtable(rows, api_key, base_id, table_name, field_ids)
            # When run by the export backend, the extension opens the tab; don't open a second one
            if not env("FROM_EXPORT_BACKEND"):
                url = f"https://airtable.com/{base_id}/{table_name}" if table_name.startswith("tbl") else f"https://airtable.com/{base_id}"
                webbrowser.open(url)
                print("Opened Airtable base in browser.", file=sys.stderr)

    print(f"Done. Total records: {len(rows)}", file=sys.stderr)


if __name__ == "__main__":
    main()
