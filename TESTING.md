# Testing the multi-tenant flow locally

## 1. Lightspeed only allows HTTPS redirect URIs

Lightspeed rejects `http://` redirect URLs. Use an **HTTPS** redirect URI that you already have (e.g. Postman’s).

In your **.env**, set:

```
LIGHTSPEED_REDIRECT_URI=https://oauth.pstmn.io/v1/callback
```

(or whatever HTTPS redirect URI is registered for your Lightspeed OAuth app). Do **not** add `http://127.0.0.1:5050/connect/callback` in Lightspeed—it will be rejected.

## 2. Backend env (server-only)

Your `.env` should have:

- `LIGHTSPEED_CLIENT_ID`, `LIGHTSPEED_CLIENT_SECRET` (app credentials; same for all users)
- `AIRTABLE_API_KEY` (one key for the server; all users’ exports use it to write to their own base/table)

You do **not** need `LIGHTSPEED_ACCOUNT_ID`, `LIGHTSPEED_ACCESS_TOKEN`, or `LIGHTSPEED_REFRESH_TOKEN` in `.env`—each user connects their own Lightspeed and chooses their own Airtable base/table.

Optional for local: `FLASK_SECRET_KEY` (defaults to a random value).

## 3. Start the backend

```bash
cd /Users/amelialane/lightspeed_to_airtable
python export_backend.py
```

You should see:

- `Backend: http://127.0.0.1:5050`
- `GET  /connect = connect your Lightspeed + Airtable (once per user)`
- `POST /api/run (connection_id, category_id) = run export (extension)`

A `connections.db` file will be created in the project directory.

## 4. Create a connection (simulate one user)

1. Open **http://127.0.0.1:5050/connect** in your browser.
2. Fill in:
   - **Lightspeed Account ID** – your account ID (e.g. from your Lightspeed/MerchantOS URL).
   - **Airtable Base ID** – the base where you want data (e.g. `appknrt32yAEr4uZh`).
   - **Airtable Table name or ID** – e.g. `Current Home` or `tblRggNPh6MLzyJ53`.
3. Click **Continue to Lightspeed login**.
4. On the next page, click **Open Lightspeed login** (opens Lightspeed in a new tab). Sign in and authorize.
5. After redirect, **copy the full URL** from the address bar (the page may be blank; the URL still has `?code=...`).
6. Back on the backend tab, **paste that URL** into the box and click **Submit**.
7. You should see “You’re connected” with a **connection key** (UUID). Copy it.

## 5. Set the connection key in the extension

1. Open **chrome://extensions**, find “Lightspeed → Airtable”, click **Options** (or right‑click the extension icon → Options).
2. Paste the connection key and click **Save**.

## 6. Test the export from Lightspeed

1. Open a Lightspeed item search page with a category, e.g.  
   `https://us.merchantos.com/?...&category_id=639&...`
2. Click the **Export to Airtable** button (bottom‑right).
3. The button should show “Exporting…” then a new tab should open to your Airtable base. Check that the table has the expected rows.

## 7. Optional: test without the extension

```bash
curl -X POST http://127.0.0.1:5050/api/run \
  -H "Content-Type: application/json" \
  -d '{"connection_id":"YOUR_CONNECTION_KEY_HERE","category_id":"639"}'
```

Use the connection key from step 4. You should get JSON with `"success": true` and `"airtable_url": "https://airtable.com/..."`.

## Troubleshooting

- **“Invalid or expired link”** – Start again from /connect. Session may have expired; fill the form and go through the paste step again.
- **“Token exchange failed”** – The pasted URL must be the one from **right after** Lightspeed redirected you (it must contain `code=...`). Paste the full URL. Ensure `LIGHTSPEED_REDIRECT_URI` in .env matches the redirect URI registered in Lightspeed (e.g. `https://oauth.pstmn.io/v1/callback`).
- **“Missing connection_id”** – You didn’t save the key in the extension Options, or the extension is using a different backend URL. Check Options and that `API_BASE` in `chrome-extension/background.js` is `http://127.0.0.1:5050`.
- **“Connection not found”** – The key was mistyped or the DB was recreated. Create a new connection at `/connect` and paste the new key.
- **Export fails (401 / auth)** – The connection’s tokens may be bad. Create a new connection at `/connect` and use the new key in the extension.
