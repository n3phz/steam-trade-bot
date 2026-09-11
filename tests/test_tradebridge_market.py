import requests
import json

print("=== TRADEBRIDGE MARKET GET TEST ===", flush=True)

with open("/run/secrets/asf_ipc_password", encoding="utf-8") as f:
    password = f.read().strip()

url = "http://steam-asf:1242/Api/TradeBridgePlugin/bot/cesarpereira27/market"

print("URL:", url, flush=True)
print("REQUEST: START", flush=True)

r = requests.get(
    url,
    headers={"Authentication": password},
    timeout=30,
)

print("REQUEST: COMPLETE", flush=True)
print("HTTP:", r.status_code, flush=True)
print("RESPONSE LENGTH:", len(r.text), flush=True)
print("RESPONSE:", r.text[:3000], flush=True)

if r.status_code != 200:
    raise SystemExit(f"HTTP {r.status_code}: {r.text}")

data = r.json()

print("=== PARSED RESPONSE ===", flush=True)
print(json.dumps(data, indent=2, ensure_ascii=False), flush=True)

assert data.get("Authenticated") is True, data
assert data.get("ContentLength", 0) > 0, data

print("=== TRADEBRIDGE MARKET GET: PASS ===", flush=True)
