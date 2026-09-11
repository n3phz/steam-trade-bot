import json
import requests

print("=== TRADEBRIDGE INVENTORY GET TEST ===", flush=True)

with open("/run/secrets/asf_ipc_password", encoding="utf-8") as f:
    password = f.read().strip()

url = "http://steam-asf:1242/Api/TradeBridgePlugin/bot/cesarpereira27/inventory"

print("URL:", url, flush=True)
print("REQUEST: START", flush=True)

response = requests.get(
    url,
    headers={"Authentication": password},
    timeout=60,
)

print("REQUEST: COMPLETE", flush=True)
print("HTTP:", response.status_code, flush=True)
print("RESPONSE LENGTH:", len(response.text), flush=True)
print("RESPONSE:", response.text[:5000], flush=True)

response.raise_for_status()

data = response.json()

print("=== INVENTORY SUMMARY ===", flush=True)
print("SUCCESS:", data.get("Success"), flush=True)
print("ITEM COUNT:", data.get("ItemCount"), flush=True)

items = data.get("Items", [])

if items:
    print("FIRST ITEM:", json.dumps(items[0], indent=2), flush=True)

assert data.get("Success") is True, data
assert data.get("ItemCount", -1) >= 0, data
assert isinstance(items, list), data

print("=== TRADEBRIDGE INVENTORY GET: PASS ===", flush=True)
