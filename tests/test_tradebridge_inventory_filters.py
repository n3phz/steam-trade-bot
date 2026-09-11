import json
import requests

with open("/run/secrets/asf_ipc_password", encoding="utf-8") as f:
    password = f.read().strip()

base = "http://steam-asf:1242/Api/TradeBridgePlugin/bot/cesarpereira27/inventory"

tests = [
    ("ALL", {}),
    ("TRADABLE ONLY", {"tradableOnly": "true"}),
    ("MARKETABLE ONLY", {"marketableOnly": "true"}),
    ("TRADABLE + MARKETABLE", {
        "tradableOnly": "true",
        "marketableOnly": "true",
    }),
]

for name, params in tests:
    print(f"=== {name} ===")

    r = requests.get(
        base,
        params=params,
        headers={"Authentication": password},
        timeout=30,
    )

    print("URL:", r.url)
    print("HTTP:", r.status_code)

    assert r.status_code == 200, r.text

    data = r.json()

    print("SUCCESS:", data.get("Success"))
    print("ITEM COUNT:", data.get("ItemCount"))

    assert data.get("Success") is True, data

    items = data.get("Items", [])

    if params.get("tradableOnly") == "true":
        assert all(item["Tradable"] for item in items), items

    if params.get("marketableOnly") == "true":
        assert all(item["Marketable"] for item in items), items

    print("PASS")
    print()

print("=== TRADEBRIDGE INVENTORY FILTERS: PASS ===")
