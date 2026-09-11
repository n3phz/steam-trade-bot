import html
import json
from urllib.parse import urlencode
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


# ============================================================
# CONFIGURATION
# ============================================================

APP_VERSION = "0.24.1"

ASF_URL = os.getenv(
    "ASF_URL",
    "http://steam-asf:1242",
)

ASF_PASSWORD_FILE = os.getenv(
    "ASF_PASSWORD_FILE",
    "/run/secrets/asf_ipc_password",
)

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/tradebot.db",
)

STEAM_APP_ID = 753
STEAM_CONTEXT_ID = 6
STEAM_CURRENCY = 3
STEAM_CURRENCY_NAME = "EUR"

BOT_NAMES = [
    "Rixqor",
    "cesarpereira27",
]

MARKET_CACHE_SECONDS = int(
    os.getenv(
        "MARKET_CACHE_SECONDS",
        "3600",
    )
)

MARKET_REQUEST_DELAY = float(
    os.getenv(
        "MARKET_REQUEST_DELAY",
        "5.0",
    )
)

STEAM_MARKET_WRITE_ENABLED = (
    os.getenv(
        "STEAM_MARKET_WRITE_ENABLED",
        "false",
    ).lower()
    in ("1", "true", "yes", "on")
)

STEAM_MARKET_LISTING_URL = (
    "https://steamcommunity.com/market/listings/"
)

MARKET_SOURCE = (
    "Steam Community Market listing page"
)


# ============================================================
# APPLICATION
# ============================================================

app = FastAPI(
    title="STEAM Trade Bot",
    version=APP_VERSION,
    description=(
        "Steam inventory monitoring and market analysis "
        "service using ArchiSteamFarm."
    ),
)


# ============================================================
# HTTP SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 "
            "(X11; Linux x86_64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }
)

_last_market_request = 0.0


# ============================================================
# DATABASE
# ============================================================

def get_db():
    directory = os.path.dirname(DB_PATH)

    if directory:
        os.makedirs(
            directory,
            exist_ok=True,
        )

    return sqlite3.connect(
        DB_PATH,
        timeout=30,
    )


def ensure_column(
    conn,
    table,
    column,
    definition,
):
    columns = {
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info({table})"
        )
    }

    if column not in columns:
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )

        return True

    return False


def init_db():
    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL,
            bot_name TEXT NOT NULL,
            app_id INTEGER NOT NULL,
            context_id INTEGER NOT NULL,
            item_count INTEGER NOT NULL,
            raw_json TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            bot_name TEXT NOT NULL,
            app_id INTEGER NOT NULL,
            context_id INTEGER NOT NULL,
            asset_id TEXT,
            class_id TEXT,
            instance_id TEXT,
            amount INTEGER NOT NULL DEFAULT 1,
            market_hash_name TEXT,
            market_name TEXT,
            type TEXT,
            tradable INTEGER NOT NULL DEFAULT 0,
            marketable INTEGER NOT NULL DEFAULT 0,
            raw_json TEXT NOT NULL,
            FOREIGN KEY(snapshot_id)
                REFERENCES inventory_snapshots(id)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_hash_name TEXT NOT NULL,
            app_id INTEGER NOT NULL,
            currency INTEGER NOT NULL,
            currency_name TEXT NOT NULL,
            lowest_price TEXT,
            median_price TEXT,
            volume TEXT,
            lowest_price_value REAL,
            median_price_value REAL,
            fetched_at TEXT NOT NULL,
            fetched_at_unix INTEGER NOT NULL,
            success INTEGER NOT NULL,
            error TEXT,
            source TEXT
        )
        """
    )

    ensure_column(
        conn,
        "market_prices",
        "source",
        "TEXT",
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_inventory_items_snapshot
        ON inventory_items(snapshot_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_inventory_items_bot
        ON inventory_items(bot_name)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_inventory_items_asset
        ON inventory_items(asset_id)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_inventory_items_market_hash
        ON inventory_items(market_hash_name)
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS
        idx_market_prices_lookup
        ON market_prices(
            market_hash_name,
            app_id,
            currency,
            fetched_at_unix
        )
        """
    )

    conn.commit()

    columns = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(market_prices)"
        )
    }

    if "source" not in columns:
        conn.close()
        raise RuntimeError(
            "Database migration failed: "
            "market_prices.source is missing"
        )

    conn.close()


# ============================================================
# ASF
# ============================================================

def get_ipc_password():
    with open(
        ASF_PASSWORD_FILE,
        "r",
        encoding="utf-8",
    ) as f:
        return f.read().strip()


def asf_get(path):
    response = requests.get(
        f"{ASF_URL}{path}",
        headers={
            "Authentication": get_ipc_password(),
        },
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    if not data.get("Success"):
        raise RuntimeError(
            data.get(
                "Message",
                "ASF request failed",
            )
        )

    return data


def get_bot_status(bot_name):
    return asf_get(
        f"/Api/Bot/{bot_name}"
    )


# ============================================================
# INVENTORY
# ============================================================

def parse_inventory(
    bot_name,
    result,
):
    assets = result.get(
        "Assets",
        [],
    )

    descriptions = result.get(
        "Descriptions",
        [],
    )

    descriptions_by_key = {}

    for description in descriptions:
        key = (
            str(
                description.get(
                    "classid",
                    "",
                )
            ),
            str(
                description.get(
                    "instanceid",
                    "",
                )
            ),
        )

        descriptions_by_key[key] = description

    items = []

    for asset in assets:
        asset_id = str(
            asset.get(
                "assetid",
                "",
            )
        )

        class_id = str(
            asset.get(
                "classid",
                "",
            )
        )

        instance_id = str(
            asset.get(
                "instanceid",
                "",
            )
        )

        amount = int(
            asset.get(
                "amount",
                1,
            )
        )

        description = descriptions_by_key.get(
            (
                class_id,
                instance_id,
            ),
            {},
        )

        items.append(
            {
                "bot_name": bot_name,
                "app_id": STEAM_APP_ID,
                "context_id": STEAM_CONTEXT_ID,
                "asset_id": asset_id,
                "class_id": class_id,
                "instance_id": instance_id,
                "amount": amount,
                "market_hash_name": description.get(
                    "market_hash_name"
                ),
                "market_name": description.get(
                    "market_name"
                ),
                "type": description.get(
                    "type"
                ),
                "tradable": bool(
                    description.get(
                        "tradable",
                        False,
                    )
                ),
                "marketable": bool(
                    description.get(
                        "marketable",
                        False,
                    )
                ),
                "raw_json": json.dumps(
                    {
                        "asset": asset,
                        "description": description,
                    },
                    ensure_ascii=False,
                ),
            }
        )

    return items


def save_snapshot(
    bot_name,
    raw_result,
    items,
):
    captured_at = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    cursor = conn.execute(
        """
        INSERT INTO inventory_snapshots (
            captured_at,
            bot_name,
            app_id,
            context_id,
            item_count,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            captured_at,
            bot_name,
            STEAM_APP_ID,
            STEAM_CONTEXT_ID,
            len(items),
            json.dumps(
                raw_result,
                ensure_ascii=False,
            ),
        ),
    )

    snapshot_id = cursor.lastrowid

    for item in items:
        conn.execute(
            """
            INSERT INTO inventory_items (
                snapshot_id,
                bot_name,
                app_id,
                context_id,
                asset_id,
                class_id,
                instance_id,
                amount,
                market_hash_name,
                market_name,
                type,
                tradable,
                marketable,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                item["bot_name"],
                item["app_id"],
                item["context_id"],
                item["asset_id"],
                item["class_id"],
                item["instance_id"],
                item["amount"],
                item["market_hash_name"],
                item["market_name"],
                item["type"],
                int(item["tradable"]),
                int(item["marketable"]),
                item["raw_json"],
            ),
        )

    conn.commit()
    conn.close()

    return snapshot_id


def fetch_inventory(bot_name):
    data = asf_get(
        f"/Api/Bot/{bot_name}/Inventory/"
        f"{STEAM_APP_ID}/"
        f"{STEAM_CONTEXT_ID}"
    )

    result = (
        data
        .get("Result", {})
        .get(bot_name)
    )

    if result is None:
        result = {
            "Assets": [],
            "Descriptions": [],
        }

    items = parse_inventory(
        bot_name,
        result,
    )

    snapshot_id = save_snapshot(
        bot_name,
        result,
        items,
    )

    return {
        "bot": bot_name,
        "app_id": STEAM_APP_ID,
        "context_id": STEAM_CONTEXT_ID,
        "snapshot_id": snapshot_id,
        "item_count": len(items),
        "items": items,
    }


# ============================================================
# SNAPSHOTS
# ============================================================

def get_latest_snapshot_id(
    conn,
    bot_name,
):
    row = conn.execute(
        """
        SELECT id
        FROM inventory_snapshots
        WHERE bot_name = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (bot_name,),
    ).fetchone()

    if not row:
        return None

    return row[0]


def get_latest_items(
    conn,
    bot_name,
):
    snapshot_id = get_latest_snapshot_id(
        conn,
        bot_name,
    )

    if snapshot_id is None:
        return []

    return conn.execute(
        """
        SELECT
            id,
            asset_id,
            class_id,
            instance_id,
            amount,
            market_hash_name,
            market_name,
            type,
            tradable,
            marketable
        FROM inventory_items
        WHERE snapshot_id = ?
        ORDER BY market_hash_name, asset_id
        """,
        (snapshot_id,),
    ).fetchall()


def latest_snapshot_ids(conn):
    rows = conn.execute(
        """
        SELECT
            bot_name,
            MAX(id)
        FROM inventory_snapshots
        GROUP BY bot_name
        """
    ).fetchall()

    return [
        row[1]
        for row in rows
        if row[1] is not None
    ]


# ============================================================
# STEAM MARKET WRITE CLIENT
# ============================================================

class SteamMarketClient:
    """
    Isolated Steam Community Market write adapter.

    0.21.0:
      - Feature flag defaults to OFF.
      - No endpoint calls are made unless explicitly enabled.
      - The client does not decide whether a proposal is safe.
      - It only performs the final Steam Market operation.
    """

    SELL_URL = (
        "https://steamcommunity.com/market/sellitem/"
    )

    def __init__(
        self,
        session,
        session_id,
        steam_id=None,
    ):
        self.session = session
        self.session_id = session_id
        self.steam_id = steam_id

    def create_listing(
        self,
        *,
        app_id,
        context_id,
        asset_id,
        amount,
        price_cents,
    ):
        """
        Create one Steam Market listing.

        price_cents is the seller-received price in the
        smallest currency unit.

        This method performs a REAL Steam write when called.
        The caller is responsible for all safety checks.
        """

        if not STEAM_MARKET_WRITE_ENABLED:
            return {
                "success": False,
                "blocked": True,
                "error":
                    "Steam Market writes are disabled.",
            }

        payload = {
            "sessionid": str(self.session_id),
            "appid": str(app_id),
            "contextid": str(context_id),
            "assetid": str(asset_id),
            "amount": str(amount),
            "price": str(price_cents),
        }

        headers = {
            "Referer": (
                "https://steamcommunity.com/"
                "my/inventory/"
            ),
            "Content-Type":
                "application/x-www-form-urlencoded",
            "X-Requested-With":
                "XMLHttpRequest",
        }

        response = self.session.post(
            self.SELL_URL,
            data=urlencode(payload),
            headers=headers,
            timeout=30,
        )

        if response.status_code != 200:
            return {
                "success": False,
                "blocked": False,
                "http_status":
                    response.status_code,
                "error":
                    "Steam Market returned HTTP "
                    f"{response.status_code}.",
                "response_text":
                    response.text[:1000],
            }

        try:
            data = response.json()
        except Exception:
            return {
                "success": False,
                "blocked": False,
                "http_status": 200,
                "error":
                    "Steam Market returned invalid JSON.",
                "response_text":
                    response.text[:1000],
            }

        if not data.get("success"):
            return {
                "success": False,
                "blocked": False,
                "http_status": 200,
                "error":
                    data.get("message")
                    or "Steam Market rejected listing.",
                "steam_response": data,
            }

        return {
            "success": True,
            "blocked": False,
            "http_status": 200,
            "listing_id":
                data.get("listingid"),
            "steam_response": data,
        }


# ============================================================
# MARKET CACHE
# ============================================================

def get_cached_market_price(
    market_hash_name,
):
    now = int(time.time())

    conn = get_db()

    row = conn.execute(
        """
        SELECT
            market_hash_name,
            app_id,
            currency,
            currency_name,
            lowest_price,
            median_price,
            volume,
            lowest_price_value,
            median_price_value,
            fetched_at,
            fetched_at_unix,
            success,
            error,
            source
        FROM market_prices
        WHERE market_hash_name = ?
        AND app_id = ?
        AND currency = ?
        AND success = 1
        AND source = ?
        ORDER BY fetched_at_unix DESC
        LIMIT 1
        """,
        (
            market_hash_name,
            STEAM_APP_ID,
            STEAM_CURRENCY,
            MARKET_SOURCE,
        ),
    ).fetchone()

    conn.close()

    if not row:
        return None

    age = now - row[10]

    if age > MARKET_CACHE_SECONDS:
        return None

    return {
        "market_hash_name": row[0],
        "app_id": row[1],
        "currency": row[2],
        "currency_name": row[3],
        "lowest_price": row[4],
        "median_price": row[5],
        "volume": row[6],
        "lowest_price_value": row[7],
        "median_price_value": row[8],
        "fetched_at": row[9],
        "fetched_at_unix": row[10],
        "success": bool(row[11]),
        "error": row[12],
        "source": row[13],
        "cached": True,
    }


def save_market_price(
    market_hash_name,
    data,
):
    now = int(time.time())

    fetched_at = datetime.now(
        timezone.utc
    ).isoformat()

    conn = get_db()

    conn.execute(
        """
        INSERT INTO market_prices (
            market_hash_name,
            app_id,
            currency,
            currency_name,
            lowest_price,
            median_price,
            volume,
            lowest_price_value,
            median_price_value,
            fetched_at,
            fetched_at_unix,
            success,
            error,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market_hash_name,
            STEAM_APP_ID,
            STEAM_CURRENCY,
            STEAM_CURRENCY_NAME,
            data.get("lowest_price"),
            data.get("median_price"),
            data.get("volume"),
            data.get("lowest_price_value"),
            data.get("median_price_value"),
            fetched_at,
            now,
            int(
                data.get(
                    "success",
                    False,
                )
            ),
            data.get("error"),
            data.get("source"),
        ),
    )

    conn.commit()
    conn.close()


# ============================================================
# PRICE PARSING
# ============================================================

def parse_price_text(value):
    if value is None:
        return None

    text = html.unescape(
        str(value)
    ).strip()

    if not text:
        return None

    text = re.sub(
        r"[^\d,.\-]",
        "",
        text,
    )

    if not text:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(
                ".",
                "",
            )
            text = text.replace(
                ",",
                ".",
            )
        else:
            text = text.replace(
                ",",
                "",
            )

    elif "," in text:
        text = text.replace(
            ",",
            ".",
        )

    try:
        return float(text)

    except ValueError:
        return None


def extract_prices_from_html(page):
    """
    Steam listing pages contain several representations of
    listing prices depending on the current Steam frontend.

    We intentionally inspect multiple known patterns rather
    than depending on a single HTML layout.
    """

    lowest_text = None
    lowest_value = None

    median_text = None
    median_value = None

    volume = None

    # --------------------------------------------------------
    # Explicit price fields
    # --------------------------------------------------------

    patterns = [
        r'"lowest_price"\s*:\s*"([^"]+)"',
        r'"lowest_price"\s*:\s*([0-9.]+)',
        r'"sell_price_text"\s*:\s*"([^"]+)"',
        r'"sell_price"\s*:\s*(\d+)',
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            page,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        value = match.group(1)

        if (
            pattern.find(
                '"sell_price"'
            ) >= 0
        ):
            try:
                lowest_value = (
                    int(value) / 100.0
                )
            except ValueError:
                pass

        else:
            parsed = parse_price_text(
                value
            )

            if parsed is not None:
                lowest_value = parsed

        lowest_text = value

        if lowest_value is not None:
            break

    # --------------------------------------------------------
    # Steam's market listing data often exposes prices in
    # g_rgListingInfo.
    # --------------------------------------------------------

    listing_match = re.search(
        r'g_rgListingInfo\s*=\s*(\{.*?\});',
        page,
        flags=re.DOTALL,
    )

    if listing_match:
        raw = listing_match.group(1)

        try:
            listing_info = json.loads(
                raw
            )

            values = []

            for listing in listing_info.values():
                if not isinstance(
                    listing,
                    dict,
                ):
                    continue

                price = listing.get(
                    "converted_price"
                )

                if price is None:
                    price = listing.get(
                        "price"
                    )

                if price is None:
                    continue

                try:
                    price_value = (
                        int(price) / 100.0
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if price_value >= 0:
                    values.append(
                        price_value
                    )

            if values:
                lowest_value = min(
                    values
                )

        except (
            ValueError,
            TypeError,
        ):
            pass

    # --------------------------------------------------------
    # Generic visible price patterns
    # --------------------------------------------------------

    if lowest_value is None:
        visible_patterns = [
            r'normal_price[^>]{0,500}?'
            r'([€$£]\s*[\d,.]+)',
            r'price[^>]{0,300}?'
            r'([€$£]\s*[\d,.]+)',
        ]

        for pattern in visible_patterns:
            match = re.search(
                pattern,
                page,
                flags=(
                    re.IGNORECASE
                    | re.DOTALL
                ),
            )

            if not match:
                continue

            candidate = match.group(1)

            parsed = parse_price_text(
                candidate
            )

            if parsed is not None:
                lowest_value = parsed
                lowest_text = candidate
                break

    # --------------------------------------------------------
    # Volume / listing count
    # --------------------------------------------------------

    volume_patterns = [
        r'"sell_listings"\s*:\s*"?(\d+)',
        r'"sell_listings"\s*:\s*(\d+)',
        r'g_rgActiveListings\s*=\s*(\d+)',
    ]

    for pattern in volume_patterns:
        match = re.search(
            pattern,
            page,
            flags=re.IGNORECASE,
        )

        if match:
            volume = match.group(1)
            break

    # --------------------------------------------------------
    # Median price
    # --------------------------------------------------------

    median_patterns = [
        r'"median_price"\s*:\s*"([^"]+)"',
        r'"median_price"\s*:\s*([0-9.]+)',
    ]

    for pattern in median_patterns:
        match = re.search(
            pattern,
            page,
            flags=re.IGNORECASE,
        )

        if not match:
            continue

        candidate = match.group(1)

        parsed = parse_price_text(
            candidate
        )

        if parsed is not None:
            median_text = candidate
            median_value = parsed
            break

    return {
        "lowest_price": lowest_text,
        "lowest_price_value": lowest_value,
        "median_price": median_text,
        "median_price_value": median_value,
        "volume": volume,
    }


# ============================================================
# STEAM LISTING PAGE
# ============================================================

def build_listing_url(
    market_hash_name,
):
    encoded = quote(
        market_hash_name,
        safe="",
    )

    return (
        f"{STEAM_MARKET_LISTING_URL}"
        f"{STEAM_APP_ID}/"
        f"{encoded}"
    )


def fetch_market_listing_page(
    market_hash_name,
):
    global _last_market_request

    elapsed = (
        time.monotonic()
        - _last_market_request
    )

    if elapsed < MARKET_REQUEST_DELAY:
        time.sleep(
            MARKET_REQUEST_DELAY
            - elapsed
        )

    url = build_listing_url(
        market_hash_name
    )

    response = session.get(
        url,
        timeout=30,
        allow_redirects=True,
    )

    _last_market_request = time.monotonic()

    return response


def fetch_market_price(
    market_hash_name,
):
    cached = get_cached_market_price(
        market_hash_name
    )

    if cached:
        return cached

    url = build_listing_url(
        market_hash_name
    )

    try:
        response = fetch_market_listing_page(
            market_hash_name
        )

        if response.status_code == 429:
            return {
                "market_hash_name": market_hash_name,
                "app_id": STEAM_APP_ID,
                "currency": STEAM_CURRENCY,
                "currency_name": STEAM_CURRENCY_NAME,
                "lowest_price": None,
                "median_price": None,
                "volume": None,
                "lowest_price_value": None,
                "median_price_value": None,
                "success": False,
                "error": (
                    "Steam Market listing page "
                    "returned HTTP 429."
                ),
                "source": MARKET_SOURCE,
                "cached": False,
                "rate_limited": True,
            }

        response.raise_for_status()

        page = response.text

        if not page.strip():
            return {
                "market_hash_name": market_hash_name,
                "app_id": STEAM_APP_ID,
                "currency": STEAM_CURRENCY,
                "currency_name": STEAM_CURRENCY_NAME,
                "lowest_price": None,
                "median_price": None,
                "volume": None,
                "lowest_price_value": None,
                "median_price_value": None,
                "success": False,
                "error": (
                    "Steam Market listing page "
                    "returned an empty response."
                ),
                "source": MARKET_SOURCE,
                "cached": False,
            }

        prices = extract_prices_from_html(
            page
        )

        if prices["lowest_price_value"] is None:
            return {
                "market_hash_name": market_hash_name,
                "app_id": STEAM_APP_ID,
                "currency": STEAM_CURRENCY,
                "currency_name": STEAM_CURRENCY_NAME,
                "lowest_price": prices[
                    "lowest_price"
                ],
                "median_price": prices[
                    "median_price"
                ],
                "volume": prices[
                    "volume"
                ],
                "lowest_price_value": None,
                "median_price_value": prices[
                    "median_price_value"
                ],
                "success": False,
                "error": (
                    "Steam listing page was "
                    "reachable, but no readable "
                    "market price was found."
                ),
                "source": MARKET_SOURCE,
                "cached": False,
            }

        result = {
            "market_hash_name": market_hash_name,
            "app_id": STEAM_APP_ID,
            "currency": STEAM_CURRENCY,
            "currency_name": STEAM_CURRENCY_NAME,
            "lowest_price": prices[
                "lowest_price"
            ],
            "median_price": prices[
                "median_price"
            ],
            "volume": prices[
                "volume"
            ],
            "lowest_price_value": prices[
                "lowest_price_value"
            ],
            "median_price_value": prices[
                "median_price_value"
            ],
            "success": True,
            "error": None,
            "source": MARKET_SOURCE,
            "cached": False,
            "listing_url": url,
        }

        save_market_price(
            market_hash_name,
            result,
        )

        return result

    except requests.RequestException as exc:
        return {
            "market_hash_name": market_hash_name,
            "app_id": STEAM_APP_ID,
            "currency": STEAM_CURRENCY,
            "currency_name": STEAM_CURRENCY_NAME,
            "lowest_price": None,
            "median_price": None,
            "volume": None,
            "lowest_price_value": None,
            "median_price_value": None,
            "success": False,
            "error": str(exc),
            "source": MARKET_SOURCE,
            "cached": False,
        }

    except Exception as exc:
        return {
            "market_hash_name": market_hash_name,
            "app_id": STEAM_APP_ID,
            "currency": STEAM_CURRENCY,
            "currency_name": STEAM_CURRENCY_NAME,
            "lowest_price": None,
            "median_price": None,
            "volume": None,
            "lowest_price_value": None,
            "median_price_value": None,
            "success": False,
            "error": str(exc),
            "source": MARKET_SOURCE,
            "cached": False,
        }


# ============================================================
# SCAN
# ============================================================

def scan_all_bots():
    results = []

    for bot_name in BOT_NAMES:
        try:
            result = fetch_inventory(
                bot_name
            )

            results.append(
                {
                    "bot": bot_name,
                    "success": True,
                    "item_count": result[
                        "item_count"
                    ],
                    "snapshot_id": result[
                        "snapshot_id"
                    ],
                }
            )

        except Exception as exc:
            results.append(
                {
                    "bot": bot_name,
                    "success": False,
                    "error": str(exc),
                }
            )

    return results


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():
    return {
        "service": "STEAM Trade Bot",
        "version": APP_VERSION,
        "language": "English",
        "mode": "read-only",
        "asf": ASF_URL,
        "steam_app_id": STEAM_APP_ID,
        "steam_context_id": STEAM_CONTEXT_ID,
        "steam_currency": STEAM_CURRENCY_NAME,
        "market_source": MARKET_SOURCE,
        "bots": BOT_NAMES,
    }


# ============================================================
# HEALTH
# ============================================================


def _market_cache_age_seconds(fetched_at):
    if not fetched_at:
        return None

    try:
        value = datetime.fromisoformat(
            fetched_at.replace("Z", "+00:00")
        )

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)

        return max(
            0,
            int(
                (
                    datetime.now(timezone.utc) - value
                ).total_seconds()
            ),
        )
    except Exception:
        return None


def _format_cache_age(seconds):
    if seconds is None:
        return None

    if seconds < 60:
        return f"{seconds}s"

    if seconds < 3600:
        return f"{seconds // 60}m"

    if seconds < 86400:
        return f"{seconds // 3600}h"

    return f"{seconds // 86400}d"


def _get_latest_inventory(bot_name):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    snapshot = conn.execute(
        """
        SELECT
            id,
            bot_name,
            captured_at
        FROM inventory_snapshots
        WHERE bot_name = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (bot_name,),
    ).fetchone()

    if not snapshot:
        conn.close()
        return None, []

    rows = conn.execute(
        """
        SELECT
            asset_id,
            class_id,
            instance_id,
            amount,
            market_hash_name,
            market_name,
            type,
            tradable,
            marketable
        FROM inventory_items
        WHERE snapshot_id = ?
        ORDER BY market_hash_name, asset_id
        """,
        (snapshot["id"],),
    ).fetchall()

    conn.close()

    return snapshot, [dict(row) for row in rows]


def _get_cached_market_prices(market_hash_names):
    if not market_hash_names:
        return {}

    placeholders = ",".join(
        "?" for _ in market_hash_names
    )

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        f"""
        SELECT
            market_hash_name,
            app_id,
            currency,
            currency_name,
            lowest_price,
            lowest_price_value,
            median_price,
            median_price_value,
            volume,
            success,
            error,
            source,
            fetched_at,
            fetched_at_unix
        FROM market_prices
        WHERE market_hash_name IN ({placeholders})
        ORDER BY id DESC
        """,
        tuple(market_hash_names),
    ).fetchall()

    conn.close()

    result = {}

    for row in rows:
        name = row["market_hash_name"]

        if name not in result:
            result[name] = dict(row)

    return result


def _build_grouped_inventory(items, prices):
    groups = {}

    for item in items:
        key = item["market_hash_name"]

        group = groups.setdefault(
            key,
            {
                "market_hash_name": key,
                "market_name": item["market_name"],
                "type": item["type"],
                "quantity": 0,
                "assets": [],
                "tradable": True,
                "marketable": True,
            },
        )

        amount = int(item.get("amount") or 0)

        group["quantity"] += amount
        group["assets"].append(item["asset_id"])

        group["tradable"] = (
            group["tradable"]
            and bool(item.get("tradable"))
        )

        group["marketable"] = (
            group["marketable"]
            and bool(item.get("marketable"))
        )

    result = []

    for key, group in groups.items():
        market = prices.get(key)

        price = None

        if market and market.get("success"):
            price = market.get("lowest_price_value")

        gross = None
        net = None

        if price is not None:
            gross = round(
                float(price) * group["quantity"],
                2,
            )

            net = round(
                gross * 0.85,
                2,
            )

        cache_age_seconds = None

        if market:
            cache_age_seconds = _market_cache_age_seconds(
                market.get("fetched_at")
            )

        result.append(
            {
                "market_hash_name":
                    group["market_hash_name"],
                "market_name":
                    group["market_name"],
                "type":
                    group["type"],
                "quantity":
                    group["quantity"],
                "copies":
                    group["quantity"],
                "assets":
                    group["assets"],
                "tradable":
                    group["tradable"],
                "marketable":
                    group["marketable"],
                "market": (
                    {
                        "lowest_price":
                            market.get("lowest_price"),
                        "lowest_price_value":
                            price,
                        "source":
                            market.get("source"),
                        "fetched_at":
                            market.get("fetched_at"),
                        "cache_age":
                            _format_cache_age(
                                cache_age_seconds
                            ),
                        "cache_age_seconds":
                            cache_age_seconds,
                        "success":
                            bool(market.get("success")),
                        "error":
                            market.get("error"),
                    }
                    if market
                    else None
                ),
                "gross_value": gross,
                "estimated_net_value": net,
            }
        )

    result.sort(
        key=lambda item: (
            item["gross_value"] is None,
            -(item["gross_value"] or 0),
            item["market_name"],
        )
    )

    return result


@app.get("/report/{bot_name}")
def report(bot_name: str):
    snapshot, items = _get_latest_inventory(bot_name)

    if not snapshot:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "bot": bot_name,
                "error":
                    "No inventory snapshot found.",
            },
        )

    market_hash_names = sorted(
        {
            item["market_hash_name"]
            for item in items
            if item.get("market_hash_name")
        }
    )

    prices = _get_cached_market_prices(
        market_hash_names
    )

    grouped = _build_grouped_inventory(
        items,
        prices,
    )

    priced = [
        item
        for item in grouped
        if item["gross_value"] is not None
    ]

    unpriced = [
        item
        for item in grouped
        if item["gross_value"] is None
    ]

    gross_value = round(
        sum(
            item["gross_value"]
            for item in priced
        ),
        2,
    )

    estimated_net_value = round(
        sum(
            item["estimated_net_value"]
            for item in priced
        ),
        2,
    )

    duplicates = [
        {
            "market_hash_name":
                item["market_hash_name"],
            "market_name":
                item["market_name"],
            "copies":
                item["quantity"],
            "value_each":
                (
                    item["market"]
                    or {}
                ).get("lowest_price_value"),
            "gross_value":
                item["gross_value"],
        }
        for item in grouped
        if item["quantity"] > 1
    ]

    cache_ages = [
        item["market"]["cache_age_seconds"]
        for item in grouped
        if item.get("market")
        and item["market"].get("cache_age_seconds")
        is not None
    ]

    oldest_cache_age = (
        max(cache_ages)
        if cache_ages
        else None
    )

    total_amount = sum(
        int(item.get("amount") or 0)
        for item in items
    )

    marketable_amount = sum(
        int(item.get("amount") or 0)
        for item in items
        if item.get("marketable")
    )

    tradable_amount = sum(
        int(item.get("amount") or 0)
        for item in items
        if item.get("tradable")
    )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "bot": bot_name,
        "snapshot_id": snapshot["id"],
        "snapshot_created_at":
            snapshot["captured_at"],
        "currency": STEAM_CURRENCY_NAME,

        "inventory": {
            "items": len(items),
            "total_amount": total_amount,
            "marketable": marketable_amount,
            "tradable": tradable_amount,
            "unique_items": len(grouped),
            "duplicate_groups": len(duplicates),
        },

        "duplicates": duplicates,

        "valuation": {
            "pricing_basis":
                "Lowest visible Steam Market listing",
            "gross_value": gross_value,
            "estimated_net_value":
                estimated_net_value,
            "fee_estimate": {
                "rate": 0.15,
                "description":
                    "Estimate only; actual Steam fees may vary.",
            },
        },

        "pricing": {
            "unique_market_items":
                len(market_hash_names),
            "priced_items":
                len(priced),
            "unpriced_items":
                len(unpriced),
            "cache_entries":
                len(prices),
            "oldest_cache_age":
                _format_cache_age(
                    oldest_cache_age
                ),
            "source":
                MARKET_SOURCE,
        },

        "unpriced": [
            {
                "market_hash_name":
                    item["market_hash_name"],
                "market_name":
                    item["market_name"],
                "quantity":
                    item["quantity"],
            }
            for item in unpriced
        ],

        "items": grouped,
    }


@app.get("/health")
def health():
    try:
        status = get_bot_status(
            "Rixqor"
        )

        bot = (
            status
            .get("Result", {})
            .get("Rixqor", {})
        )

        return {
            "status": "ok",
            "version": APP_VERSION,
            "asf": "ok",
            "asf_http_status": 200,
            "bot_connected": bot.get(
                "IsConnectedAndLoggedOn",
                False,
            ),
        }

    except Exception as exc:
        return {
            "status": "error",
            "version": APP_VERSION,
            "asf": "error",
            "error": str(exc),
        }


# ============================================================
# SCAN
# ============================================================

@app.get("/scan")
def scan():
    return {
        "status": "ok",
        "version": APP_VERSION,
        "results": scan_all_bots(),
    }


# ============================================================
# ASF
# ============================================================

@app.get("/asf/{bot_name}")
def bot_status(
    bot_name: str,
):
    try:
        return {
            "status": "ok",
            "bot": bot_name,
            "result": get_bot_status(
                bot_name
            ),
        }

    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ASF HTTP error: {exc}",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


# ============================================================
# INVENTORY
# ============================================================



# ============================================================
# MARKET OPPORTUNITIES
# ============================================================

def _classify_market_opportunity(item):
    """
    Classify an inventory group and produce a read-only
    sale-intelligence decision.

    Decision order:
      1. NOT_MARKETABLE
      2. UNPRICED
      3. STALE_PRICE
      4. DUPLICATE_NOT_TRADABLE
      5. SELL_DUPLICATE
      6. KEEP_SINGLE

    This function NEVER executes a market action.
    """

    quantity = int(item.get("quantity") or 0)
    marketable = bool(item.get("marketable"))
    tradable = bool(item.get("tradable"))
    market = item.get("market") or {}

    price = market.get("lowest_price_value")

    priced = (
        price is not None
        and bool(market.get("success"))
    )

    cache_age = market.get("cache_age_seconds")

    price_freshness = "UNKNOWN"

    if cache_age is not None:
        if cache_age <= 21600:
            price_freshness = "FRESH"
        elif cache_age <= 86400:
            price_freshness = "STALE"
        else:
            price_freshness = "VERY_STALE"

    gross_value = item.get("gross_value")
    estimated_net_value = item.get(
        "estimated_net_value"
    )

    net_value_per_copy = None

    if (
        estimated_net_value is not None
        and quantity > 0
    ):
        net_value_per_copy = round(
            float(estimated_net_value) / quantity,
            2,
        )

    # Keep one copy of every item and treat only the
    # excess copies as potentially sellable.
    keep_quantity = 1 if quantity > 0 else 0
    sell_quantity = max(
        0,
        quantity - keep_quantity,
    )

    sell_gross_value = None
    sell_estimated_net_value = None

    if price is not None and sell_quantity > 0:
        sell_gross_value = round(
            float(price) * sell_quantity,
            2,
        )

        sell_estimated_net_value = round(
            sell_gross_value * 0.85,
            2,
        )

    base = {
        "recommendation": "KEEP",
        "economic_score": 0,
        "net_value_per_copy": net_value_per_copy,
        "price_freshness": price_freshness,
        "keep_quantity": keep_quantity,
        "sell_quantity": sell_quantity,
        "sell_gross_value": sell_gross_value,
        "sell_estimated_net_value":
            sell_estimated_net_value,
    }

    if not marketable:
        return {
            **base,
            "classification": "NOT_MARKETABLE",
            "confidence": "HIGH",
            "priority": 0,
            "recommendation": "KEEP",
            "reason":
                "Item is not marketable on Steam.",
        }

    if not priced:
        return {
            **base,
            "classification": "UNPRICED",
            "confidence": "LOW",
            "priority": 0,
            "recommendation": "REVIEW",
            "reason":
                "No valid cached Steam Market price.",
        }

    if (
        cache_age is not None
        and cache_age > 21600
    ):
        return {
            **base,
            "classification": "STALE_PRICE",
            "confidence": "LOW",
            "priority": 1,
            "recommendation": "REFRESH_PRICE",
            "reason":
                "Cached market price is older than 6 hours.",
        }

    if quantity >= 2 and not tradable:
        return {
            **base,
            "classification":
                "DUPLICATE_NOT_TRADABLE",
            "confidence": "HIGH",
            "priority": 1,
            "recommendation": "WAIT",
            "reason":
                "Duplicate copies exist but the item is not tradable.",
        }

    if quantity >= 2 and tradable:
        economic_score = 50

        if net_value_per_copy is not None:
            if net_value_per_copy >= 0.50:
                economic_score += 40
            elif net_value_per_copy >= 0.20:
                economic_score += 30
            elif net_value_per_copy >= 0.10:
                economic_score += 20
            elif net_value_per_copy >= 0.05:
                economic_score += 10

        economic_score = min(
            100,
            economic_score,
        )

        return {
            **base,
            "classification": "SELL_DUPLICATE",
            "confidence": "HIGH",
            "priority": 3,
            "recommendation": "SELL",
            "economic_score": economic_score,
            "reason":
                f"{quantity} tradable marketable copies with a valid fresh market price.",
        }

    return {
        **base,
        "classification": "KEEP_SINGLE",
        "confidence": "MEDIUM",
        "priority": 0,
        "recommendation": "KEEP",
        "reason":
            "Single copy; no duplicate sale opportunity.",
    }


def _build_market_opportunities(bot_name):
    snapshot, items = _get_latest_inventory(bot_name)

    if not snapshot:
        return None

    market_hash_names = sorted(
        {
            item["market_hash_name"]
            for item in items
            if item.get("market_hash_name")
        }
    )

    # IMPORTANT:
    # This function only reads the existing price cache.
    # It never queries Steam Market.
    prices = _get_cached_market_prices(
        market_hash_names
    )

    grouped = _build_grouped_inventory(
        items,
        prices,
    )

    opportunities = []

    for item in grouped:
        decision = _classify_market_opportunity(
            item
        )

        opportunity = {
            "market_hash_name":
                item["market_hash_name"],
            "market_name":
                item["market_name"],
            "type":
                item["type"],
            "quantity":
                item["quantity"],
            "copies":
                item["copies"],
            "keep_quantity":
                decision["keep_quantity"],
            "sell_quantity":
                decision["sell_quantity"],
            "sell_gross_value":
                decision["sell_gross_value"],
            "sell_estimated_net_value":
                decision["sell_estimated_net_value"],
            "tradable":
                item["tradable"],
            "marketable":
                item["marketable"],
            "classification":
                decision["classification"],
            "confidence":
                decision["confidence"],
            "priority":
                decision["priority"],
            "reason":
                decision["reason"],
            "recommendation":
                decision["recommendation"],
            "economic_score":
                decision["economic_score"],
            "net_value_per_copy":
                decision["net_value_per_copy"],
            "price_freshness":
                decision["price_freshness"],
            "market":
                item["market"],
            "gross_value":
                item["gross_value"],
            "estimated_net_value":
                item["estimated_net_value"],
        }

        opportunities.append(opportunity)

    # Highest-value candidates first.
    opportunities.sort(
        key=lambda item: (
            item["classification"] != "SELL_DUPLICATE",
            item["gross_value"] is None,
            -(item["gross_value"] or 0),
            item["market_name"] or "",
        )
    )

    sell_candidates = [
        item
        for item in opportunities
        if item["classification"] == "SELL_DUPLICATE"
    ]

    priced = [
        item
        for item in opportunities
        if item["gross_value"] is not None
    ]

    gross_value = round(
        sum(
            item["gross_value"]
            for item in priced
        ),
        2,
    )

    estimated_net_value = round(
        sum(
            item["estimated_net_value"]
            for item in priced
        ),
        2,
    )

    sellable_excess_quantity = sum(
        int(item.get("sell_quantity") or 0)
        for item in sell_candidates
    )

    candidate_gross = round(
        sum(
            item.get("sell_gross_value") or 0
            for item in sell_candidates
        ),
        2,
    )

    candidate_net = round(
        sum(
            item.get("sell_estimated_net_value") or 0
            for item in sell_candidates
        ),
        2,
    )

    counts = {}

    for item in opportunities:
        classification = item["classification"]
        counts[classification] = (
            counts.get(classification, 0) + 1
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "bot": bot_name,
        "snapshot_id": snapshot["id"],
        "snapshot_captured_at":
            snapshot["captured_at"],
        "currency": STEAM_CURRENCY_NAME,

        "pricing_basis":
            "Lowest visible Steam Market listing",

        "read_only": True,

        "execution": {
            "enabled": False,
            "mode": "read-only",
        },

        "intelligence": {
            "version": APP_VERSION,
            "sale_execution": False,
            "pricing_source":
                "Cached Steam Community Market listing page",
        },

        "classification": {
            "SELL_DUPLICATE":
                "Duplicate, marketable and tradable item.",
            "KEEP_SINGLE":
                "Single copy with no duplicate sale opportunity.",
            "DUPLICATE_NOT_TRADABLE":
                "Duplicate item that cannot currently be traded.",
            "STALE_PRICE":
                "Cached market price is older than 6 hours.",
            "UNPRICED":
                "Marketable item without a valid cached price.",
            "NOT_MARKETABLE":
                "Item cannot currently be listed on Steam Market.",
        },

        "counts": counts,

        "totals": {
            "gross_value": gross_value,
            "estimated_net_value":
                estimated_net_value,

            "sellable_excess_quantity":
                sellable_excess_quantity,

            "candidate_gross_value":
                candidate_gross,

            "candidate_estimated_net_value":
                candidate_net,
        },

        "sell_candidates": sell_candidates,

        "items": opportunities,
    }




# ============================================================
# DRY-RUN SALE EXECUTION
# ============================================================

def _build_sale_dry_run(bot_name: str):
    """
    Build a completely read-only simulation of the sale
    recommendations for a bot.

    IMPORTANT:
      - No Steam Market write operation is performed.
      - No listing is created.
      - No asset is transferred.
      - Only the latest inventory snapshot is used.
    """

    snapshot, items = _get_latest_inventory(bot_name)

    if not snapshot:
        return None

    market_hash_names = sorted(
        {
            item["market_hash_name"]
            for item in items
            if item.get("market_hash_name")
        }
    )

    prices = _get_cached_market_prices(
        market_hash_names
    )

    grouped = _build_grouped_inventory(
        items,
        prices,
    )

    simulations = []

    for group in grouped:
        decision = _classify_market_opportunity(
            group
        )

        sell_quantity = int(
            decision.get("sell_quantity") or 0
        )

        if (
            decision.get("recommendation") != "SELL"
            or sell_quantity <= 0
        ):
            continue

        market_hash_name = group.get(
            "market_hash_name"
        )

        # Only assets belonging to the CURRENT
        # inventory snapshot are considered.
        current_assets = [
            item
            for item in items
            if item.get("market_hash_name")
            == market_hash_name
        ]

        current_assets.sort(
            key=lambda item: (
                str(item.get("asset_id") or "")
            )
        )

        selected_assets = current_assets[
            :sell_quantity
        ]

        if len(selected_assets) < sell_quantity:
            simulations.append(
                {
                    "market_hash_name":
                        market_hash_name,
                    "market_name":
                        group.get("market_name"),
                    "status":
                        "ERROR",
                    "executed":
                        False,
                    "error":
                        "Latest snapshot does not contain enough assets for the recommended sale quantity.",
                    "requested_quantity":
                        sell_quantity,
                    "available_assets":
                        len(current_assets),
                }
            )

            continue

        market = group.get("market") or {}

        price = market.get(
            "lowest_price_value"
        )

        gross = decision.get(
            "sell_gross_value"
        )

        net = decision.get(
            "sell_estimated_net_value"
        )

        simulations.append(
            {
                "market_hash_name":
                    market_hash_name,

                "market_name":
                    group.get("market_name"),

                "type":
                    group.get("type"),

                "snapshot_id":
                    snapshot["id"],

                "quantity_owned":
                    group.get("quantity"),

                "keep_quantity":
                    decision.get(
                        "keep_quantity"
                    ),

                "sell_quantity":
                    sell_quantity,

                "assets":
                    [
                        {
                            "asset_id":
                                item.get("asset_id"),
                            "class_id":
                                item.get("class_id"),
                            "instance_id":
                                item.get("instance_id"),
                            "amount":
                                item.get("amount"),
                        }
                        for item in selected_assets
                    ],

                "price":
                    price,

                "price_display":
                    market.get(
                        "lowest_price"
                    ),

                "gross_value":
                    gross,

                "estimated_net_value":
                    net,

                "recommendation":
                    decision.get(
                        "recommendation"
                    ),

                "classification":
                    decision.get(
                        "classification"
                    ),

                "confidence":
                    decision.get(
                        "confidence"
                    ),

                "economic_score":
                    decision.get(
                        "economic_score"
                    ),

                "price_freshness":
                    decision.get(
                        "price_freshness"
                    ),

                "status":
                    "DRY_RUN",

                "executed":
                    False,

                "execution_mode":
                    "dry-run",

                "execution_allowed":
                    False,

                "reason":
                    "Simulation only. No Steam Market operation was performed.",
            }
        )


    gross_total = round(
        sum(
            item.get("gross_value") or 0
            for item in simulations
            if item.get("status") == "DRY_RUN"
        ),
        2,
    )

    net_total = round(
        sum(
            item.get("estimated_net_value") or 0
            for item in simulations
            if item.get("status") == "DRY_RUN"
        ),
        2,
    )

    quantity_total = sum(
        int(item.get("sell_quantity") or 0)
        for item in simulations
        if item.get("status") == "DRY_RUN"
    )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "bot": bot_name,

        "snapshot_id":
            snapshot["id"],

        "snapshot_captured_at":
            snapshot["captured_at"],

        "currency":
            STEAM_CURRENCY_NAME,

        "read_only":
            True,

        "execution": {
            "enabled": False,
            "mode": "dry-run",
            "executed": False,
            "steam_write_operation": False,
        },

        "simulation": {
            "status": "DRY_RUN",
            "sale_execution": False,
            "assets_selected":
                quantity_total,
            "gross_value":
                gross_total,
            "estimated_net_value":
                net_total,
        },

        "sales":
            simulations,
    }


@app.get("/sell/dry-run/{bot_name}")
def sell_dry_run(bot_name: str):
    result = _build_sale_dry_run(
        bot_name
    )

    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "bot": bot_name,
                "error":
                    "No inventory snapshot found.",
            },
        )

    return result


# ============================================================
# SALE PROPOSALS
# ============================================================

# ============================================================
# SALE PROPOSAL PERSISTENCE
# ============================================================

def migrate_sale_proposals():
    """
    Create the sale proposal persistence table.

    This migration is additive only.
    Existing inventory, snapshot and market tables are untouched.
    """

    conn = get_db()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sale_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL UNIQUE,
            bot_name TEXT NOT NULL,
            snapshot_id INTEGER,
            market_hash_name TEXT,
            market_name TEXT,
            asset_ids_json TEXT NOT NULL,
            sell_quantity INTEGER NOT NULL,
            price REAL,
            gross_value REAL,
            estimated_net_value REAL,
            recommendation TEXT,
            classification TEXT,
            confidence TEXT,
            economic_score INTEGER,
            price_freshness TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',
            reason TEXT,
            created_at TEXT NOT NULL,
            approved_at TEXT,
            rejected_at TEXT,
            executed_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def _persist_sale_proposal(proposal):
    """
    Persist a proposal idempotently.

    Existing proposal IDs are never overwritten automatically.
    """

    migrate_sale_proposals()

    conn = get_db()

    proposal_id = proposal["proposal_id"]

    existing = conn.execute(
        """
        SELECT
            id,
            proposal_id,
            status,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()

    if existing:
        conn.close()

        return {
            "id": existing[0],
            "proposal_id": existing[1],
            "status": existing[2],
            "created_at": existing[3],
            "approved_at": existing[4],
            "rejected_at": existing[5],
            "executed_at": existing[6],
            "created": False,
        }

    now = datetime.now(timezone.utc).isoformat()

    conn.execute(
        """
        INSERT INTO sale_proposals (
            proposal_id,
            bot_name,
            snapshot_id,
            market_hash_name,
            market_name,
            asset_ids_json,
            sell_quantity,
            price,
            gross_value,
            estimated_net_value,
            recommendation,
            classification,
            confidence,
            economic_score,
            price_freshness,
            status,
            reason,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal["proposal_id"],
            proposal["bot"],
            proposal.get("snapshot_id"),
            proposal.get("market_hash_name"),
            proposal.get("market_name"),
            json.dumps(
                proposal.get("asset_ids", []),
                ensure_ascii=False,
            ),
            int(proposal.get("sell_quantity") or 0),
            proposal.get("price"),
            proposal.get("gross_value"),
            proposal.get("estimated_net_value"),
            proposal.get("recommendation"),
            proposal.get("classification"),
            proposal.get("confidence"),
            proposal.get("economic_score"),
            proposal.get("price_freshness"),
            "PENDING",
            proposal.get("reason"),
            now,
        ),
    )

    conn.commit()

    row = conn.execute(
        """
        SELECT
            id,
            proposal_id,
            status,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()

    conn.close()

    return {
        "id": row[0],
        "proposal_id": row[1],
        "status": row[2],
        "created_at": row[3],
        "approved_at": row[4],
        "rejected_at": row[5],
        "executed_at": row[6],
        "created": True,
    }


def _get_sale_proposals(bot_name=None, status=None):
    migrate_sale_proposals()

    conn = get_db()

    where = []
    params = []

    if bot_name:
        where.append("bot_name = ?")
        params.append(bot_name)

    if status:
        where.append("status = ?")
        params.append(status)

    where_sql = ""

    if where:
        where_sql = "WHERE " + " AND ".join(where)

    rows = conn.execute(
        f"""
        SELECT
            id,
            proposal_id,
            bot_name,
            snapshot_id,
            market_hash_name,
            market_name,
            asset_ids_json,
            sell_quantity,
            price,
            gross_value,
            estimated_net_value,
            recommendation,
            classification,
            confidence,
            economic_score,
            price_freshness,
            status,
            reason,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        {where_sql}
        ORDER BY id DESC
        """,
        tuple(params),
    ).fetchall()

    conn.close()

    return [
        {
            "id": row[0],
            "proposal_id": row[1],
            "bot": row[2],
            "snapshot_id": row[3],
            "market_hash_name": row[4],
            "market_name": row[5],
            "asset_ids": json.loads(row[6] or "[]"),
            "sell_quantity": row[7],
            "price": row[8],
            "gross_value": row[9],
            "estimated_net_value": row[10],
            "recommendation": row[11],
            "classification": row[12],
            "confidence": row[13],
            "economic_score": row[14],
            "price_freshness": row[15],
            "status": row[16],
            "reason": row[17],
            "created_at": row[18],
            "approved_at": row[19],
            "rejected_at": row[20],
            "executed_at": row[21],
        }
        for row in rows
    ]



def _build_sale_proposals(bot_name: str):
    """
    Build read-only sale proposals from the current
    dry-run result.

    IMPORTANT:
      - No Steam Market write operation is performed.
      - No listing is created.
      - No asset is transferred.
      - Proposals are informational only.
    """

    dry_run = _build_sale_dry_run(bot_name)

    if dry_run is None:
        return None

    proposals = []

    for sale in dry_run.get("sales", []):
        if sale.get("status") != "DRY_RUN":
            continue

        asset_ids = [
            asset.get("asset_id")
            for asset in sale.get("assets", [])
            if asset.get("asset_id") is not None
        ]

        proposal_id = (
            f"{bot_name}:"
            f"{dry_run.get('snapshot_id')}:"
            f"{sale.get('market_hash_name')}:"
            f"{','.join(asset_ids)}"
        )

        proposal = {
                "proposal_id": proposal_id,

                "status": "PROPOSED",

                "bot": bot_name,

                "snapshot_id":
                    sale.get("snapshot_id"),

                "market_hash_name":
                    sale.get("market_hash_name"),

                "market_name":
                    sale.get("market_name"),

                "type":
                    sale.get("type"),

                "quantity_owned":
                    sale.get("quantity_owned"),

                "keep_quantity":
                    sale.get("keep_quantity"),

                "sell_quantity":
                    sale.get("sell_quantity"),

                "assets":
                    sale.get("assets", []),

                "asset_ids":
                    asset_ids,

                "price":
                    sale.get("price"),

                "price_display":
                    sale.get("price_display"),

                "gross_value":
                    sale.get("gross_value"),

                "estimated_net_value":
                    sale.get("estimated_net_value"),

                "recommendation":
                    sale.get("recommendation"),

                "classification":
                    sale.get("classification"),

                "confidence":
                    sale.get("confidence"),

                "economic_score":
                    sale.get("economic_score"),

                "price_freshness":
                    sale.get("price_freshness"),

                "execution": {
                    "allowed": False,
                    "executed": False,
                    "mode": "proposal",
                    "steam_write_operation": False,
                },

                "reason":
                    "Sale proposal only. "
                    "No Steam Market operation was performed.",
            }

        proposals.append(proposal)

        _persist_sale_proposal(proposal)

    gross_total = round(
        sum(
            item.get("gross_value") or 0
            for item in proposals
        ),
        2,
    )

    net_total = round(
        sum(
            item.get("estimated_net_value") or 0
            for item in proposals
        ),
        2,
    )

    quantity_total = sum(
        int(item.get("sell_quantity") or 0)
        for item in proposals
    )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "bot": bot_name,

        "snapshot_id":
            dry_run.get("snapshot_id"),

        "snapshot_captured_at":
            dry_run.get("snapshot_captured_at"),

        "currency":
            dry_run.get("currency"),

        "read_only": True,

        "execution": {
            "enabled": False,
            "mode": "proposal",
            "executed": False,
            "steam_write_operation": False,
        },

        "proposal": {
            "status": "PROPOSED",
            "approval_required": True,
            "execution_allowed": False,
        },

        "totals": {
            "proposals":
                len(proposals),
            "sell_quantity":
                quantity_total,
            "gross_value":
                gross_total,
            "estimated_net_value":
                net_total,
        },

        "proposals":
            proposals,
    }


# ============================================================
# SALE PROPOSAL APPROVAL WORKFLOW
# ============================================================

def _update_sale_proposal_status(
    proposal_id: str,
    new_status: str,
):
    """
    Perform a state transition on a persisted sale proposal.

    Allowed transitions:

        PENDING -> APPROVED
        PENDING -> REJECTED

    No Steam operation is performed here.
    """

    if new_status not in {"APPROVED", "REJECTED"}:
        raise ValueError(
            f"Invalid proposal status: {new_status}"
        )

    migrate_sale_proposals()

    conn = get_db()

    row = conn.execute(
        """
        SELECT
            id,
            proposal_id,
            status,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()

    if not row:
        conn.close()

        return {
            "status": "NOT_FOUND",
            "proposal_id": proposal_id,
            "changed": False,
        }

    current_status = row[2]

    # --------------------------------------------------------
    # Idempotent repeat of the same operation.
    # --------------------------------------------------------

    if current_status == new_status:
        conn.close()

        return {
            "status": current_status,
            "proposal_id": proposal_id,
            "changed": False,
            "idempotent": True,
        }

    # --------------------------------------------------------
    # Only PENDING may transition.
    # --------------------------------------------------------

    if current_status != "PENDING":
        conn.close()

        return {
            "status": current_status,
            "proposal_id": proposal_id,
            "changed": False,
            "error": (
                f"Cannot transition proposal from "
                f"{current_status} to {new_status}."
            ),
        }

    now = datetime.now(timezone.utc).isoformat()

    if new_status == "APPROVED":
        conn.execute(
            """
            UPDATE sale_proposals
            SET
                status = 'APPROVED',
                approved_at = ?
            WHERE proposal_id = ?
            AND status = 'PENDING'
            """,
            (now, proposal_id),
        )

    else:
        conn.execute(
            """
            UPDATE sale_proposals
            SET
                status = 'REJECTED',
                rejected_at = ?
            WHERE proposal_id = ?
            AND status = 'PENDING'
            """,
            (now, proposal_id),
        )

    conn.commit()

    updated = conn.execute(
        """
        SELECT
            id,
            proposal_id,
            status,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()

    conn.close()

    return {
        "status": updated[2],
        "proposal_id": updated[1],
        "changed": True,
        "idempotent": False,
        "created_at": updated[3],
        "approved_at": updated[4],
        "rejected_at": updated[5],
        "executed_at": updated[6],
    }


@app.post("/sell/proposals/{proposal_id}/approve")
def approve_sale_proposal(proposal_id: str):
    result = _update_sale_proposal_status(
        proposal_id,
        "APPROVED",
    )

    if result.get("status") == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "proposal_id": proposal_id,
                "error": "Sale proposal not found.",
            },
        )

    if result.get("error"):
        return JSONResponse(
            status_code=409,
            content={
                "status": "error",
                "proposal_id": proposal_id,
                "current_status":
                    result.get("status"),
                "error":
                    result.get("error"),
            },
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "proposal_id": proposal_id,

        "proposal": {
            "status": result["status"],
            "changed": result["changed"],
            "idempotent":
                result.get("idempotent", False),
        },

        "approval": {
            "approved": True,
            "approved_at":
                result.get("approved_at"),
        },

        "execution": {
            "enabled": False,
            "allowed": False,
            "executed": False,
            "steam_write_operation": False,
            "mode": "approval-only",
        },

        "message":
            "Proposal approved. "
            "No Steam Market operation was performed.",
    }


@app.post("/sell/proposals/{proposal_id}/reject")
def reject_sale_proposal(proposal_id: str):
    result = _update_sale_proposal_status(
        proposal_id,
        "REJECTED",
    )

    if result.get("status") == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "proposal_id": proposal_id,
                "error": "Sale proposal not found.",
            },
        )

    if result.get("error"):
        return JSONResponse(
            status_code=409,
            content={
                "status": "error",
                "proposal_id": proposal_id,
                "current_status":
                    result.get("status"),
                "error":
                    result.get("error"),
            },
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "proposal_id": proposal_id,

        "proposal": {
            "status": result["status"],
            "changed": result["changed"],
            "idempotent":
                result.get("idempotent", False),
        },

        "rejection": {
            "rejected": True,
            "rejected_at":
                result.get("rejected_at"),
        },

        "execution": {
            "enabled": False,
            "allowed": False,
            "executed": False,
            "steam_write_operation": False,
            "mode": "approval-only",
        },

        "message":
            "Proposal rejected. "
            "No Steam Market operation was performed.",
    }



# ============================================================
# EXECUTION GATE
# ============================================================

def _check_sale_execution_gate(proposal_id: str):
    """
    Check whether a sale proposal is eligible to enter
    the execution stage.

    IMPORTANT:
      - This function performs NO Steam operation.
      - No listing is created.
      - No asset is transferred.
      - No database status is changed.
      - APPROVED remains APPROVED.
    """

    migrate_sale_proposals()

    conn = get_db()

    row = conn.execute(
        """
        SELECT
            id,
            proposal_id,
            bot_name,
            snapshot_id,
            market_hash_name,
            market_name,
            asset_ids_json,
            sell_quantity,
            price,
            gross_value,
            estimated_net_value,
            recommendation,
            classification,
            confidence,
            economic_score,
            price_freshness,
            status,
            reason,
            created_at,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        LIMIT 1
        """,
        (proposal_id,),
    ).fetchone()

    conn.close()

    if row is None:
        return {
            "status": "NOT_FOUND",
            "proposal_id": proposal_id,
        }

    status = row[16]

    proposal = {
        "id": row[0],
        "proposal_id": row[1],
        "bot_name": row[2],
        "snapshot_id": row[3],
        "market_hash_name": row[4],
        "market_name": row[5],
        "asset_ids": json.loads(row[6] or "[]"),
        "sell_quantity": row[7],
        "price": row[8],
        "gross_value": row[9],
        "estimated_net_value": row[10],
        "recommendation": row[11],
        "classification": row[12],
        "confidence": row[13],
        "economic_score": row[14],
        "price_freshness": row[15],
        "status": status,
        "reason": row[17],
        "created_at": row[18],
        "approved_at": row[19],
        "rejected_at": row[20],
        "executed_at": row[21],
    }

    if status == "APPROVED":
        return {
            "status": "READY_TO_EXECUTE",
            "proposal": proposal,
            "execution": {
                "enabled": True,
                "allowed": True,
                "executed": False,
                "steam_write_operation": False,
                "mode": "gate-only",
            },
        }

    if status == "PENDING":
        return {
            "status": "NOT_READY",
            "proposal": proposal,
            "execution": {
                "enabled": False,
                "allowed": False,
                "executed": False,
                "steam_write_operation": False,
                "mode": "gate-only",
            },
            "error":
                "Proposal must be approved before execution.",
        }

    if status == "REJECTED":
        return {
            "status": "NOT_READY",
            "proposal": proposal,
            "execution": {
                "enabled": False,
                "allowed": False,
                "executed": False,
                "steam_write_operation": False,
                "mode": "gate-only",
            },
            "error":
                "Rejected proposal cannot enter execution.",
        }

    if status == "EXECUTED":
        return {
            "status": "ALREADY_EXECUTED",
            "proposal": proposal,
            "execution": {
                "enabled": False,
                "allowed": False,
                "executed": True,
                "steam_write_operation": False,
                "mode": "gate-only",
            },
            "error":
                "Proposal has already been executed.",
        }

    return {
        "status": "NOT_READY",
        "proposal": proposal,
        "execution": {
            "enabled": False,
            "allowed": False,
            "executed": False,
            "steam_write_operation": False,
            "mode": "gate-only",
        },
        "error":
            f"Unsupported proposal status: {status}",
    }



# ============================================================
# SANDBOX EXECUTION ADAPTER
# ============================================================

def _build_sandbox_execution(proposal_id: str):
    """
    Build the exact sale operation that would be submitted
    to Steam, without performing the operation.

    IMPORTANT:
      - No Steam Market write operation.
      - No listing is created.
      - No asset is transferred.
      - No proposal status is changed.
    """

    gate = _check_sale_execution_gate(
        proposal_id
    )

    if gate.get("status") != "READY_TO_EXECUTE":
        return gate

    proposal = gate["proposal"]

    asset_ids = proposal.get("asset_ids") or []
    sell_quantity = int(
        proposal.get("sell_quantity") or 0
    )
    price = proposal.get("price")

    # --------------------------------------------------------
    # SAFETY VALIDATION
    # --------------------------------------------------------

    if not asset_ids:
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error": "No asset IDs are attached to the proposal.",
        }

    if sell_quantity <= 0:
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error": "Sell quantity must be greater than zero.",
        }

    if sell_quantity != len(asset_ids):
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error":
                "Sell quantity does not match selected assets.",
        }

    if price is None:
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error": "Proposal has no valid price.",
        }

    if float(price) <= 0:
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error": "Proposal price must be greater than zero.",
        }

    if proposal.get("classification") != "SELL_DUPLICATE":
        return {
            "status": "BLOCKED",
            "proposal_id": proposal_id,
            "error":
                "Only SELL_DUPLICATE proposals may enter execution.",
        }

    # --------------------------------------------------------
    # LIVE INVENTORY REVALIDATION
    # --------------------------------------------------------

    inventory_check = (
        _revalidate_sale_proposal_inventory(
            proposal
        )
    )

    if inventory_check["status"] != "VALID":
        return {
            "status": "STALE_ASSET",
            "proposal_id": proposal_id,
            "execution": {
                "enabled": False,
                "allowed": False,
                "executed": False,
                "steam_write_operation": False,
                "mode": "sandbox",
            },
            "inventory_revalidation":
                inventory_check,
            "error":
                inventory_check.get(
                    "error",
                    "Inventory revalidation failed.",
                ),
        }

    # --------------------------------------------------------
    # DETERMINISTIC EXECUTION ID
    # --------------------------------------------------------

    execution_id = (
        f"sandbox:"
        f"{proposal_id}:"
        f"{','.join(asset_ids)}:"
        f"{price}"
    )

    operation = {
        "action": "CREATE_MARKET_LISTING",

        "market_hash_name":
            proposal.get("market_hash_name"),

        "market_name":
            proposal.get("market_name"),

        "asset_ids":
            asset_ids,

        "quantity":
            sell_quantity,

        "price":
            float(price),

        "currency":
            STEAM_CURRENCY_NAME,

        "gross_value":
            proposal.get("gross_value"),

        "estimated_net_value":
            proposal.get("estimated_net_value"),
    }

    return {
        "status": "SANDBOX_READY",

        "proposal_id":
            proposal_id,

        "execution_id":
            execution_id,

        "execution": {
            "enabled": True,
            "allowed": True,
            "executed": False,
            "steam_write_operation": False,
            "mode": "sandbox",
        },

        "operation":
            operation,

        "inventory_revalidation":
            inventory_check,

        "safety": {
            "proposal_approved": True,
            "already_executed": False,
            "asset_ids_present": True,
            "quantity_valid": True,
            "price_valid": True,
            "classification_valid": True,
            "inventory_revalidated": True,
            "inventory_snapshot_id":
                inventory_check.get(
                    "latest_snapshot_id"
                ),
        },

        "message":
            "Sandbox execution plan created. "
            "No Steam Market operation was performed.",
    }




def _revalidate_sale_proposal_inventory(proposal):
    """
    Revalidate proposal assets against the latest inventory
    snapshot before execution.

    No Steam Market write operation is performed here.
    """

    bot_name = proposal.get("bot_name")
    proposal_snapshot_id = proposal.get("snapshot_id")
    market_hash_name = proposal.get(
        "market_hash_name"
    )

    sell_quantity = int(
        proposal.get("sell_quantity") or 0
    )

    asset_ids = [
        str(asset_id)
        for asset_id in (
            proposal.get("asset_ids") or []
        )
    ]

    if not bot_name:
        return {
            "status": "BLOCKED",
            "error": "Proposal bot is missing.",
        }

    if not market_hash_name:
        return {
            "status": "BLOCKED",
            "error":
                "Proposal market_hash_name is missing.",
        }

    if sell_quantity <= 0:
        return {
            "status": "BLOCKED",
            "error":
                "Proposal sell quantity must be greater than zero.",
        }

    if not asset_ids:
        return {
            "status": "BLOCKED",
            "error":
                "Proposal contains no asset IDs.",
        }

    if sell_quantity != len(asset_ids):
        return {
            "status": "BLOCKED",
            "error":
                "Proposal quantity does not match asset count.",
            "sell_quantity": sell_quantity,
            "asset_count": len(asset_ids),
        }

    conn = get_db()

    latest = conn.execute(
        """
        SELECT
            id,
            captured_at,
            bot_name,
            item_count
        FROM inventory_snapshots
        WHERE bot_name = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (bot_name,),
    ).fetchone()

    if latest is None:
        conn.close()

        return {
            "status": "BLOCKED",
            "error":
                "No inventory snapshot exists for proposal bot.",
        }

    latest_snapshot_id = latest[0]

    placeholders = ",".join(
        "?" for _ in asset_ids
    )

    rows = conn.execute(
        f"""
        SELECT
            asset_id,
            amount,
            market_hash_name,
            market_name,
            tradable,
            marketable
        FROM inventory_items
        WHERE snapshot_id = ?
          AND bot_name = ?
          AND asset_id IN ({placeholders})
        """,
        (
            latest_snapshot_id,
            bot_name,
            *asset_ids,
        ),
    ).fetchall()

    conn.close()

    by_asset = {
        str(row[0]): {
            "asset_id": str(row[0]),
            "amount": int(row[1] or 0),
            "market_hash_name": row[2],
            "market_name": row[3],
            "tradable": bool(row[4]),
            "marketable": bool(row[5]),
        }
        for row in rows
    }

    missing = [
        asset_id
        for asset_id in asset_ids
        if asset_id not in by_asset
    ]

    if missing:
        return {
            "status": "BLOCKED",
            "error":
                "One or more proposal assets are no longer "
                "present in the latest inventory snapshot.",
            "missing_asset_ids": missing,
            "latest_snapshot_id":
                latest_snapshot_id,
        }

    invalid = []

    for asset_id in asset_ids:
        item = by_asset[asset_id]

        if item["market_hash_name"] != market_hash_name:
            invalid.append({
                "asset_id": asset_id,
                "reason": "market_hash_name_mismatch",
                "current_market_hash_name":
                    item["market_hash_name"],
                "proposal_market_hash_name":
                    market_hash_name,
            })
            continue

        if item["amount"] <= 0:
            invalid.append({
                "asset_id": asset_id,
                "reason": "invalid_amount",
                "amount": item["amount"],
            })
            continue

        if not item["tradable"]:
            invalid.append({
                "asset_id": asset_id,
                "reason": "not_tradable",
            })
            continue

        if not item["marketable"]:
            invalid.append({
                "asset_id": asset_id,
                "reason": "not_marketable",
            })
            continue

    if invalid:
        return {
            "status": "BLOCKED",
            "error":
                "One or more proposal assets failed "
                "inventory revalidation.",
            "latest_snapshot_id":
                latest_snapshot_id,
            "invalid_assets": invalid,
        }

    return {
        "status": "VALID",
        "proposal_snapshot_id":
            proposal_snapshot_id,
        "latest_snapshot_id":
            latest_snapshot_id,
        "latest_snapshot_captured_at":
            latest[1],
        "asset_ids":
            asset_ids,
        "sell_quantity":
            sell_quantity,
        "assets": [
            by_asset[asset_id]
            for asset_id in asset_ids
        ],
    }


@app.post("/sell/proposals/{proposal_id}/execute/sandbox")
def execute_sale_proposal_sandbox(
    proposal_id: str,
):
    """
    Generate a sandbox execution plan.

    This endpoint NEVER performs a Steam write operation.
    """

    result = _build_sandbox_execution(
        proposal_id
    )

    status = result.get("status")

    if status == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Sale proposal not found.",
            },
        )

    if status != "SANDBOX_READY":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "execution": {
                    "enabled": False,
                    "allowed": False,
                    "executed": False,
                    "steam_write_operation": False,
                    "mode": "sandbox",
                },
                "result": result,
            },
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "proposal_id": proposal_id,
        "result": result,
    }


def _build_sale_execution_contract(
    proposal,
    asset_ids,
    sell_quantity,
    price,
):
    """
    Build the common Steam Market execution contract.

    This function NEVER performs a Steam write operation.
    It only describes the operation that an executor may
    eventually perform.
    """

    return {
        "action":
            "CREATE_MARKET_LISTING",

        "market_hash_name":
            proposal.get("market_hash_name"),

        "market_name":
            proposal.get("market_name"),

        "asset_ids":
            asset_ids,

        "quantity":
            sell_quantity,

        "price":
            price,

        "currency":
            STEAM_CURRENCY_NAME,

        "gross_value":
            proposal.get("gross_value"),

        "estimated_net_value":
            proposal.get("estimated_net_value"),
    }


def _execute_sale_contract_real(
    operation,
):
    """
    Real Steam Market execution boundary.

    This function is deliberately protected by
    STEAM_MARKET_WRITE_ENABLED.

    With writes disabled, it MUST NOT invoke any
    Steam Market write operation.
    """

    if not STEAM_MARKET_WRITE_ENABLED:
        return {
            "status":
                "REAL_EXECUTION_BLOCKED",

            "execution": {
                "enabled": False,
                "allowed": False,
                "executed": False,
                "steam_write_operation": False,
                "mode": "real",
            },

            "steam_response": {
                "success": False,
                "blocked": True,
                "listing_created": False,
                "external_listing_id": None,
            },

            "error":
                "Steam Market writes are disabled.",
        }

    return {
        "status":
            "REAL_EXECUTION_NOT_IMPLEMENTED",

        "execution": {
            "enabled": False,
            "allowed": False,
            "executed": False,
            "steam_write_operation": False,
            "mode": "real",
        },

        "steam_response": {
            "success": False,
            "blocked": True,
            "listing_created": False,
            "external_listing_id": None,
        },

        "error":
            "Real Steam Market execution is not yet enabled.",
    }


@app.post("/sell/proposals/{proposal_id}/execute/mock")
def execute_sale_proposal_mock(
    proposal_id: str,
):
    """
    Generate a deterministic mock Steam Market execution.

    This endpoint NEVER performs a Steam write operation.
    It only validates the proposal and produces the operation
    contract that a future real executor would receive.
    """

    gate = _check_sale_execution_gate(
        proposal_id
    )

    if gate["status"] == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Sale proposal not found.",
            },
        )

    if gate["status"] != "READY_TO_EXECUTE":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "execution": {
                    "enabled": False,
                    "allowed": False,
                    "executed": False,
                    "steam_write_operation": False,
                    "mode": "mock",
                },
                "gate": gate,
            },
        )

    proposal = gate["proposal"]

    # --------------------------------------------------------
    # LIVE INVENTORY REVALIDATION
    # --------------------------------------------------------

    inventory_check = (
        _revalidate_sale_proposal_inventory(
            proposal
        )
    )

    if inventory_check["status"] != "VALID":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "result": {
                    "status": "STALE_ASSET",

                    "execution": {
                        "enabled": False,
                        "allowed": False,
                        "executed": False,
                        "steam_write_operation": False,
                        "mode": "mock",
                    },

                    "inventory_revalidation":
                        inventory_check,

                    "error":
                        inventory_check.get(
                            "error",
                            "Inventory revalidation failed.",
                        ),
                },
            },
        )

    # --------------------------------------------------------
    # PROPOSAL VALIDATION
    # --------------------------------------------------------

    asset_ids = [
        str(asset_id)
        for asset_id in (
            proposal.get("asset_ids") or []
        )
    ]

    sell_quantity = int(
        proposal.get("sell_quantity") or 0
    )

    price = float(
        proposal.get("price") or 0
    )

    if not asset_ids:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal contains no asset IDs.",
            },
        )

    if sell_quantity <= 0:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal quantity must be greater than zero.",
            },
        )

    if price <= 0:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal price must be greater than zero.",
            },
        )

    if proposal.get("classification") != "SELL_DUPLICATE":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Only SELL_DUPLICATE proposals may enter mock execution.",
            },
        )

    # --------------------------------------------------------
    # DETERMINISTIC MOCK EXECUTION ID
    # --------------------------------------------------------

    execution_id = (
        f"mock:"
        f"{proposal_id}:"
        f"{','.join(asset_ids)}:"
        f"{price}"
    )

    operation = _build_sale_execution_contract(
        proposal,
        asset_ids,
        sell_quantity,
        price,
    )

    return {
        "status":
            "MOCK_READY",

        "proposal_id":
            proposal_id,

        "execution_id":
            execution_id,

        "execution": {
            "enabled": True,
            "allowed": True,
            "executed": False,
            "steam_write_operation": False,
            "mode": "mock",
        },

        "operation":
            operation,

        "inventory_revalidation": {
            "status": "VALID",
            "latest_snapshot_id":
                inventory_check.get(
                    "latest_snapshot_id"
                ),
            "asset_ids":
                inventory_check.get(
                    "asset_ids"
                ),
            "sell_quantity":
                inventory_check.get(
                    "sell_quantity"
                ),
        },

        "mock_response": {
            "success": True,
            "listing_created": False,
            "external_listing_id": None,
        },

        "safety": {
            "proposal_approved": True,
            "inventory_revalidated": True,
            "already_executed": False,
            "steam_write_operation": False,
        },

        "message":
            "Mock Steam Market execution completed. "
            "No Steam Market operation was performed.",
    }



@app.post("/sell/proposals/{proposal_id}/execute/real")
def execute_sale_proposal_real(
    proposal_id: str,
):
    """
    Execute a sale proposal through the real executor boundary.

    The real executor remains protected by
    STEAM_MARKET_WRITE_ENABLED.
    """

    gate = _check_sale_execution_gate(
        proposal_id
    )

    if gate["status"] == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Sale proposal not found.",
            },
        )

    if gate["status"] != "READY_TO_EXECUTE":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "execution": {
                    "enabled": False,
                    "allowed": False,
                    "executed": False,
                    "steam_write_operation": False,
                    "mode": "real",
                },
                "gate": gate,
            },
        )

    proposal = gate["proposal"]

    # --------------------------------------------------------
    # LIVE INVENTORY REVALIDATION
    # --------------------------------------------------------

    inventory_check = (
        _revalidate_sale_proposal_inventory(
            proposal
        )
    )

    if inventory_check["status"] != "VALID":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "result": {
                    "status": "STALE_ASSET",

                    "execution": {
                        "enabled": False,
                        "allowed": False,
                        "executed": False,
                        "steam_write_operation": False,
                        "mode": "real",
                    },

                    "inventory_revalidation":
                        inventory_check,

                    "error":
                        inventory_check.get(
                            "error",
                            "Inventory revalidation failed.",
                        ),
                },
            },
        )

    # --------------------------------------------------------
    # BUILD EXECUTION CONTRACT
    # --------------------------------------------------------

    asset_ids = [
        str(asset_id)
        for asset_id in (
            proposal.get("asset_ids") or []
        )
    ]

    sell_quantity = int(
        proposal.get("sell_quantity") or 0
    )

    price = float(
        proposal.get("price") or 0
    )

    if not asset_ids:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal contains no asset IDs.",
            },
        )

    if sell_quantity <= 0:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal quantity must be greater than zero.",
            },
        )

    if price <= 0:
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Proposal price must be greater than zero.",
            },
        )

    if proposal.get("classification") != "SELL_DUPLICATE":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "error":
                    "Only SELL_DUPLICATE proposals may enter real execution.",
            },
        )

    operation = _build_sale_execution_contract(
        proposal,
        asset_ids,
        sell_quantity,
        price,
    )

    # --------------------------------------------------------
    # REAL EXECUTOR
    # --------------------------------------------------------

    executor_result = _execute_sale_contract_real(
        operation
    )

    if executor_result["status"] != "REAL_EXECUTION_READY":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "result": executor_result,
            },
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "proposal_id": proposal_id,
        "result": executor_result,
    }


@app.post("/sell/proposals/{proposal_id}/execute")
def execute_sale_proposal(proposal_id: str):
    """
    Execution gate only.

    0.19.0 deliberately does NOT execute a Steam Market
    operation. It only verifies whether an APPROVED proposal
    is eligible for the future executor.
    """

    result = _check_sale_execution_gate(proposal_id)

    if result["status"] == "NOT_FOUND":
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "proposal_id": proposal_id,
                "error": "Sale proposal not found.",
            },
        )

    if result["status"] != "READY_TO_EXECUTE":
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "version": APP_VERSION,
                "proposal_id": proposal_id,
                "gate": result,
            },
        )

    return {
        "status": "ok",
        "version": APP_VERSION,
        "proposal_id": proposal_id,

        "gate": {
            "status": "READY_TO_EXECUTE",
            "approval_required": True,
            "approved": True,
            "execution_allowed": True,
        },

        "execution": {
            "enabled": True,
            "allowed": True,
            "executed": False,
            "steam_write_operation": False,
            "mode": "gate-only",
        },

        "proposal": result["proposal"],

        "message":
            "Execution gate passed. "
            "No Steam Market operation was performed.",
    }


@app.get("/sell/proposals")
def sale_proposals_all():
    proposals = _get_sale_proposals()

    return {
        "status": "ok",
        "version": APP_VERSION,
        "read_only": True,
        "execution": {
            "enabled": False,
            "executed": False,
            "steam_write_operation": False,
        },
        "proposals": proposals,
    }


@app.get("/sell/proposals/{bot_name}")
def sell_proposals(bot_name: str):
    result = _build_sale_proposals(bot_name)

    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "bot": bot_name,
                "error":
                    "No inventory snapshot found.",
            },
        )

    persisted = _get_sale_proposals(
        bot_name=bot_name,
    )

    result["persistence"] = {
        "enabled": True,
        "status": "PERSISTED",
        "count": len(persisted),
    }

    result["proposals"] = persisted

    result["execution"] = {
        "enabled": False,
        "mode": "proposal",
        "executed": False,
        "steam_write_operation": False,
    }

    return result


@app.get("/opportunities")
def opportunities_all():
    results = []

    for bot_name in BOT_NAMES:
        result = _build_market_opportunities(bot_name)

        if result is not None:
            results.append(result)

    return {
        "status": "ok",
        "version": APP_VERSION,
        "currency": STEAM_CURRENCY_NAME,
        "read_only": True,
        "accounts": results,
    }


@app.get("/opportunities/{bot_name}")
def opportunities_bot(bot_name: str):
    result = _build_market_opportunities(bot_name)

    if result is None:
        return JSONResponse(
            status_code=404,
            content={
                "status": "error",
                "bot": bot_name,
                "error":
                    "No inventory snapshot found.",
            },
        )

    return result


@app.get("/inventory/{bot_name}")
def inventory(
    bot_name: str,
):
    try:
        return {
            "status": "ok",
            **fetch_inventory(
                bot_name
            ),
        }

    except requests.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"ASF HTTP error: {exc}",
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        )


# ============================================================
# ITEMS
# ============================================================

@app.get("/items/{bot_name}")
def items(
    bot_name: str,
):
    conn = get_db()

    rows = get_latest_items(
        conn,
        bot_name,
    )

    snapshot_id = get_latest_snapshot_id(
        conn,
        bot_name,
    )

    conn.close()

    return {
        "status": "ok",
        "bot": bot_name,
        "snapshot_id": snapshot_id,
        "item_count": len(rows),
        "items": [
            {
                "asset_id": row[1],
                "class_id": row[2],
                "instance_id": row[3],
                "amount": row[4],
                "market_hash_name": row[5],
                "market_name": row[6],
                "type": row[7],
                "tradable": bool(row[8]),
                "marketable": bool(row[9]),
            }
            for row in rows
        ],
    }


# ============================================================
# MARKETABLE
# ============================================================

@app.get("/marketable/{bot_name}")
def marketable(
    bot_name: str,
):
    conn = get_db()

    rows = [
        row
        for row in get_latest_items(
            conn,
            bot_name,
        )
        if row[8] == 1
        and row[9] == 1
    ]

    snapshot_id = get_latest_snapshot_id(
        conn,
        bot_name,
    )

    conn.close()

    return {
        "status": "ok",
        "bot": bot_name,
        "snapshot_id": snapshot_id,
        "item_count": len(rows),
        "items": [
            {
                "asset_id": row[1],
                "class_id": row[2],
                "instance_id": row[3],
                "amount": row[4],
                "market_hash_name": row[5],
                "market_name": row[6],
                "type": row[7],
                "tradable": bool(row[8]),
                "marketable": bool(row[9]),
            }
            for row in rows
        ],
    }


# ============================================================
# PRICE
# ============================================================

@app.get("/price/{market_hash_name:path}")
def price(
    market_hash_name: str,
):
    result = fetch_market_price(
        market_hash_name
    )

    if result.get(
        "rate_limited"
    ):
        return {
            "status": "rate_limited",
            **result,
        }

    return {
        "status": (
            "ok"
            if result["success"]
            else "error"
        ),
        **result,
    }


# ============================================================
# PRICES
# ============================================================

@app.get("/prices/{bot_name}")
def prices(
    bot_name: str,
):
    conn = get_db()

    rows = get_latest_items(
        conn,
        bot_name,
    )

    conn.close()

    names = []

    for row in rows:
        if (
            row[8] != 1
            or row[9] != 1
        ):
            continue

        name = row[5]

        if (
            name
            and name not in names
        ):
            names.append(name)

    results = []

    for name in names:
        results.append(
            fetch_market_price(
                name
            )
        )

    return {
        "status": "ok",
        "bot": bot_name,
        "currency": STEAM_CURRENCY_NAME,
        "item_count": len(results),
        "prices": results,
    }


# ============================================================
# VALUATION
# ============================================================

@app.get("/valuation/{bot_name}")
def valuation(
    bot_name: str,
):
    conn = get_db()

    rows = get_latest_items(
        conn,
        bot_name,
    )

    snapshot_id = get_latest_snapshot_id(
        conn,
        bot_name,
    )

    conn.close()

    items = []

    gross_total = 0.0
    net_total = 0.0
    priced_items = 0

    for row in rows:
        if (
            row[8] != 1
            or row[9] != 1
            or not row[5]
        ):
            continue

        market_hash_name = row[5]

        market = fetch_market_price(
            market_hash_name
        )

        amount = row[4]

        unit_price = market.get(
            "lowest_price_value"
        )

        gross_value = None
        net_value = None

        if unit_price is not None:
            gross_value = round(
                unit_price * amount,
                2,
            )

            net_value = round(
                gross_value * 0.85,
                2,
            )

            gross_total += gross_value
            net_total += net_value

            priced_items += 1

        items.append(
            {
                "asset_id": row[1],
                "market_hash_name": row[5],
                "market_name": row[6],
                "type": row[7],
                "amount": amount,
                "market": market,
                "gross_value": gross_value,
                "estimated_net_value": net_value,
            }
        )

    return {
        "status": "ok",
        "bot": bot_name,
        "snapshot_id": snapshot_id,
        "currency": STEAM_CURRENCY_NAME,
        "pricing_basis": (
            "Lowest visible Steam Market listing"
        ),
        "fee_estimate": {
            "rate": 0.15,
            "description": (
                "Estimate only; actual Steam fees "
                "may vary."
            ),
        },
        "priced_items": priced_items,
        "item_count": len(items),
        "totals": {
            "gross_value": round(
                gross_total,
                2,
            ),
            "estimated_net_value": round(
                net_total,
                2,
            ),
        },
        "items": items,
    }


# ============================================================
# DUPLICATES
# ============================================================

@app.get("/duplicates")
def duplicates():
    conn = get_db()

    latest_ids = latest_snapshot_ids(
        conn
    )

    if not latest_ids:
        conn.close()

        return {
            "status": "ok",
            "duplicate_count": 0,
            "duplicates": [],
        }

    placeholders = ",".join(
        "?"
        for _ in latest_ids
    )

    rows = conn.execute(
        f"""
        SELECT
            market_hash_name,
            SUM(amount) AS total_amount
        FROM inventory_items
        WHERE market_hash_name IS NOT NULL
        AND tradable = 1
        AND snapshot_id IN ({placeholders})
        GROUP BY market_hash_name
        HAVING SUM(amount) > 1
        ORDER BY total_amount DESC,
                 market_hash_name ASC
        """,
        latest_ids,
    ).fetchall()

    conn.close()

    return {
        "status": "ok",
        "duplicate_count": len(rows),
        "duplicates": [
            {
                "market_hash_name": row[0],
                "total_amount": row[1],
            }
            for row in rows
        ],
    }


# ============================================================
# SUMMARY
# ============================================================

@app.get("/summary")
def summary():
    conn = get_db()

    latest_ids = latest_snapshot_ids(
        conn
    )

    if not latest_ids:
        conn.close()

        return {
            "status": "ok",
            "version": APP_VERSION,
            "accounts": [],
            "total_items": 0,
            "total_marketable": 0,
            "total_duplicates": 0,
        }

    placeholders = ",".join(
        "?"
        for _ in latest_ids
    )

    account_rows = conn.execute(
        f"""
        SELECT
            bot_name,
            COUNT(*) AS item_count,
            COALESCE(SUM(amount), 0),
            COALESCE(
                SUM(
                    CASE
                        WHEN tradable = 1
                        AND marketable = 1
                        THEN amount
                        ELSE 0
                    END
                ),
                0
            )
        FROM inventory_items
        WHERE snapshot_id IN ({placeholders})
        GROUP BY bot_name
        ORDER BY bot_name
        """,
        latest_ids,
    ).fetchall()

    duplicate_rows = conn.execute(
        f"""
        SELECT
            market_hash_name,
            SUM(amount)
        FROM inventory_items
        WHERE snapshot_id IN ({placeholders})
        AND tradable = 1
        AND market_hash_name IS NOT NULL
        GROUP BY market_hash_name
        HAVING SUM(amount) > 1
        """,
        latest_ids,
    ).fetchall()

    conn.close()

    return {
        "status": "ok",
        "version": APP_VERSION,
        "accounts": [
            {
                "bot": row[0],
                "item_count": row[1],
                "total_amount": row[2],
                "marketable_amount": row[3],
            }
            for row in account_rows
        ],
        "total_items": sum(
            row[1]
            for row in account_rows
        ),
        "total_marketable": sum(
            row[3]
            for row in account_rows
        ),
        "total_duplicates": len(
            duplicate_rows
        ),
    }


# ============================================================
# SNAPSHOTS
# ============================================================

@app.get("/snapshots/{bot_name}")
def snapshots(
    bot_name: str,
):
    conn = get_db()

    rows = conn.execute(
        """
        SELECT
            id,
            captured_at,
            bot_name,
            app_id,
            context_id,
            item_count
        FROM inventory_snapshots
        WHERE bot_name = ?
        ORDER BY id DESC
        LIMIT 100
        """,
        (bot_name,),
    ).fetchall()

    conn.close()

    return {
        "status": "ok",
        "bot": bot_name,
        "snapshots": [
            {
                "id": row[0],
                "captured_at": row[1],
                "bot_name": row[2],
                "app_id": row[3],
                "context_id": row[4],
                "item_count": row[5],
            }
            for row in rows
        ],
    }


# ============================================================
# DATABASE
# ============================================================

@app.get("/database")
def database():
    conn = get_db()

    snapshot_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM inventory_snapshots
        """
    ).fetchone()[0]

    item_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM inventory_items
        """
    ).fetchone()[0]

    market_price_count = conn.execute(
        """
        SELECT COUNT(*)
        FROM market_prices
        """
    ).fetchone()[0]

    market_columns = [
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(market_prices)"
        )
    ]

    conn.close()

    return {
        "status": "ok",
        "database": DB_PATH,
        "snapshot_count": snapshot_count,
        "item_count": item_count,
        "market_price_count": market_price_count,
        "schema": {
            "market_prices_source": (
                "source" in market_columns
            ),
        },
    }
