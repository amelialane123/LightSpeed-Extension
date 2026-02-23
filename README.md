# Lightspeed R-Series → Airtable Export

Export all items from **Lightspeed Retail (R-Series)** (name, cost, price, vendor, SKUs, and optional images) into **JSON** and **CSV** suitable for importing into Airtable or other tools. Built for large catalogs: **pagination** is handled automatically (e.g. 18,000+ items).

## Project layout

```
lightspeed_to_airtable/
├── README.md
├── requirements.txt
├── pyproject.toml
├── config.example.env    # copy to .env and fill in credentials
├── lightspeed_export.py  # main script
├── output/               # export output (CSV/JSON) written here by default
└── .gitignore
```

## What you get

| Field         | Source in Lightspeed              |
|---------------|-----------------------------------|
| **name**      | Item description                  |
| **cost**      | Item default cost                 |
| **price**     | Default retail price (useType Default) |
| **vendor_name** | Default vendor name (from Vendor) |
| **vendor_id** | defaultVendorID                   |
| **systemSku** | System SKU (barcode)              |
| **customSku** | Custom SKU                        |
| **upc**       | UPC                               |
| **image_urls** | (optional) If `--with-images` is used |

## Setup

1. **Python 3.9+** and pip.

2. **Install dependencies:**
   ```bash
   cd lightspeed_to_airtable
   pip install -r requirements.txt
   ```
   Or install the project as a package (adds `lightspeed-export` command):
   ```bash
   pip install -e .
   ```

3. **Lightspeed API credentials:**
   - Register an OAuth app at [Lightspeed OAuth registration](https://cloud.lightspeedapp.com/oauth/register.php).
   - Complete the OAuth flow to get an **access token** and **refresh token**.
   - Your **Account ID** is the numeric account ID in the Lightspeed Retail URL or from the API.

4. **Environment variables** (or use flags):
   - Copy `config.example.env` to `.env` and fill in:
     - `LIGHTSPEED_ACCOUNT_ID` – Your Lightspeed account ID (required)
     - **For automatic token refresh** (recommended for 18k+ items):
       - `LIGHTSPEED_REFRESH_TOKEN` – OAuth refresh token
       - `LIGHTSPEED_CLIENT_ID` – OAuth client ID
       - `LIGHTSPEED_CLIENT_SECRET` – OAuth client secret  
       When these are set, the script refreshes the access token automatically on 401, so long exports complete without manual re-auth.
     - **Alternatively**, use only `LIGHTSPEED_ACCESS_TOKEN` for short runs (token expires in ~30 minutes).

## Usage

Export to both JSON and CSV (default output directory `output/`):

```bash
python lightspeed_export.py
```

Options:

- **`-o DIR`** / **`--output-dir DIR`** – Write files to `DIR` (default: `output`).
- **`-f json`** / **`-f csv`** / **`-f both`** – Output format (default: both).
- **`--airtable-json`** – Write JSON in Airtable’s “create records” shape: `{"records": [{"fields": {...}}, ...]}` for use with the Airtable API.
- **`--with-images`** – Load the Image relation on items (higher API usage and slower; images may also live on ItemMatrix for matrix items).
- **`--access-token TOKEN`** / **`--account-id ID`** – Override env vars.
- **`--refresh-token`** / **`--client-id`** / **`--client-secret`** – Override refresh credentials (for auto-refresh on 401).

Examples:

```bash
# Default: CSV + JSON to output/
python lightspeed_export.py

# JSON only, Airtable-style, to ./out
python lightspeed_export.py -o out -f json --airtable-json

# Include image URLs (more API calls)
python lightspeed_export.py --with-images
```

## Pagination

The script uses the Lightspeed V3 **cursor-based pagination** (max 100 records per request). It follows the `next` URL in each response until no more pages, so all items are fetched without you specifying page count.

## Importing into Airtable

1. **CSV:** In Airtable, create or open a base and use **Import** → **CSV file** and select `output/lightspeed_items.csv`. Map columns to your table fields (name, cost, price, vendor_name, etc.).

2. **JSON (Airtable API):** Use the file generated with `--airtable-json`. Each element in `records` has a `fields` object; you can POST batches to [Airtable’s create records endpoint](https://airtable.com/developers/web/api/create-records) (e.g. up to 10 records per request; stay under rate limits with small delays between batches).

## Notes

- **Rate limits:** A short delay (0.2s) is applied between paginated requests to reduce the chance of hitting Lightspeed rate limits.
- **Token refresh:** If you set `LIGHTSPEED_REFRESH_TOKEN`, `LIGHTSPEED_CLIENT_ID`, and `LIGHTSPEED_CLIENT_SECRET`, the script will refresh the access token automatically when the API returns 401 (e.g. after ~30 minutes). You can run without ever setting `LIGHTSPEED_ACCESS_TOKEN`; the script will get an initial token from the refresh token.
- **Images:** Item-level images are loaded only with `--with-images`. For matrix items, images are often on the **ItemMatrix**; this script does not fetch ItemMatrix images. You can extend it to call the ItemMatrix Image endpoint if needed.
