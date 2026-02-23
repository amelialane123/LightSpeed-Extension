# Deploy the backend (Railway or Render)

Deploy the Flask backend so the Chrome extension can call it from anywhere. These steps use **Railway** (recommended); **Render** is an alternative.

---

## Prerequisites

1. **Lightspeed OAuth app**  
   At [Lightspeed OAuth](https://cloud.lightspeedapp.com/oauth/), your app must allow an **HTTPS redirect URI**. You’ll set this to your deployed backend URL (e.g. `https://your-app.railway.app/connect/callback`) after deployment.

2. **Git**  
   Your project in a Git repo (e.g. GitHub) so the host can clone and build it.

---

## Option A: Railway

### 1. Create a Railway account and project

- Go to [railway.app](https://railway.app) and sign in (e.g. with GitHub).
- Click **New Project** → **Deploy from GitHub repo**.
- Connect GitHub (if needed) and select the `lightspeed_to_airtable` repo (or the repo that contains the backend).
- Choose the repo; Railway will try to detect the app. If it doesn’t, you’ll configure it in the next step.

### 2. Configure the service

- In the project, open the service that was created.
- **Settings** (or **Variables**):
  - **Root Directory**: leave blank if the backend is at the repo root; otherwise set the folder that contains `export_backend.py` and `lightspeed_export.py`.
  - **Build Command**: leave default (Railway often detects Python).
  - **Start Command**: set to:
    ```bash
    python export_backend.py
    ```
    (or `python3 export_backend.py` if your image uses that.)
- **Settings → Networking**: add a **Public URL** (e.g. “Generate domain”) so the app gets a URL like `https://lightspeed-to-airtable-production-xxxx.up.railway.app`.

### 3. Set environment variables

In the same service, open **Variables** (or **Environment**) and add every variable your backend needs. **Do not** commit `.env` to the repo; set these in the Railway dashboard.

| Variable | Required | Example / notes |
|----------|----------|------------------|
| `LIGHTSPEED_CLIENT_ID` | Yes | From your Lightspeed OAuth app |
| `LIGHTSPEED_CLIENT_SECRET` | Yes | From your Lightspeed OAuth app |
| `LIGHTSPEED_REDIRECT_URI` | Yes | `https://YOUR-RAILWAY-URL/connect/callback` (see step 4) |
| `BACKEND_PUBLIC_URL` | Recommended | `https://YOUR-RAILWAY-URL` (no trailing slash) |
| `AIRTABLE_API_KEY` | Yes* | Airtable personal access token (data.records:read + data.records:write, correct base) |
| `FLASK_SECRET_KEY` | Yes | Random string (e.g. `openssl rand -hex 32`) |

\* Each user can also supply their own Airtable API key when connecting; the server can use one shared key if you prefer.

Optional:

- `CONNECTIONS_DB` – leave unset to use the default path (SQLite file in the app directory). On Railway the filesystem is ephemeral, so the DB resets on redeploy unless you add a **Volume** and set this path to the volume path.
- `PORT` – Railway sets this automatically; don’t override unless you have a reason.

### 4. Set the redirect URI in Lightspeed

- Copy the **public URL** of your app (e.g. `https://lightspeed-to-airtable-production-xxxx.up.railway.app`).
- In Railway **Variables**, set:
  - `LIGHTSPEED_REDIRECT_URI` = `https://YOUR-RAILWAY-URL/connect/callback`
  - `BACKEND_PUBLIC_URL` = `https://YOUR-RAILWAY-URL`
- In the [Lightspeed OAuth](https://cloud.lightspeedapp.com/oauth/) app settings, add **exactly** that redirect URI (`https://YOUR-RAILWAY-URL/connect/callback`) to the allowed redirect URIs and save.

### 5. Deploy and test

- Commit and push any changes (e.g. `requirements.txt`, `export_backend.py`). Railway will redeploy if the repo is connected.
- Or trigger a deploy from the Railway dashboard.
- Open `https://YOUR-RAILWAY-URL/connect` in a browser. You should see the connect page.
- Run through the connect flow once (Lightspeed + Airtable) and confirm you get a connection key and that the extension can run an export.

### 6. (Optional) Persist the database with a Volume

- In the service, add a **Volume** and mount it at a path (e.g. `/data`).
- In **Variables**, set `CONNECTIONS_DB=/data/connections.db`.
- Redeploy. The SQLite file will live on the volume and survive redeploys.

---

## Option B: Render

### 1. Create a Web Service

- Go to [render.com](https://render.com) and sign in.
- **New** → **Web Service**.
- Connect your GitHub repo and select the repo containing the backend.

### 2. Configure build and start

- **Runtime**: Python 3.
- **Build Command**:  
  `pip install -r requirements.txt`
- **Start Command**:  
  `python export_backend.py`
- **Instance Type**: Free or paid.

### 3. Set environment variables

Under **Environment**, add the same variables as in the Railway table above:

- `LIGHTSPEED_CLIENT_ID`
- `LIGHTSPEED_CLIENT_SECRET`
- `LIGHTSPEED_REDIRECT_URI` = `https://YOUR-RENDER-URL.onrender.com/connect/callback`
- `BACKEND_PUBLIC_URL` = `https://YOUR-RENDER-URL.onrender.com`
- `AIRTABLE_API_KEY`
- `FLASK_SECRET_KEY`

Render sets `PORT` for you.

### 4. Deploy and set Lightspeed redirect

- Create the service; Render will assign a URL like `https://lightspeed-to-airtable.onrender.com`.
- Set `LIGHTSPEED_REDIRECT_URI` and `BACKEND_PUBLIC_URL` to that URL (and `/connect/callback` for the redirect).
- In Lightspeed OAuth, add `https://YOUR-RENDER-URL.onrender.com/connect/callback` as an allowed redirect URI.
- Test `/connect` and the extension.

**Note:** On the free tier, Render may spin down the app after inactivity; the first request after that can be slow.

---

## After deployment

1. **Extension**: In `chrome-extension/background.js` and `chrome-extension/options.js`, set `API_BASE` (or your config) to your deployed URL, e.g. `https://your-app.railway.app`.
2. **Users**: Send them the extension (Chrome Web Store or zip) and the link to your backend’s `/connect` page so they can connect their Lightspeed and Airtable and get a connection key.

---

## Troubleshooting

- **429 / rate limits**: Lightspeed and Airtable rate-limit; the app already backs off. If you see 429 often, consider increasing delays in `lightspeed_export.py`.
- **OAuth “redirect_uri mismatch”**: The redirect URI in Lightspeed must match **exactly** (including `https`, path `/connect/callback`, no trailing slash).
- **DB resets on redeploy**: Use a Railway Volume (or Render persistent disk if available) and `CONNECTIONS_DB` so the SQLite file is stored on persistent storage.
