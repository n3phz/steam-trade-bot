import requests
import traceback

print("=== TRADEBRIDGE STATUS TEST ===", flush=True)

try:
    with open("/run/secrets/asf_ipc_password", encoding="utf-8") as f:
        password = f.read().strip()

    print("SECRET: READ", flush=True)
    print("SECRET LENGTH:", len(password), flush=True)

    url = "http://steam-asf:1242/Api/TradeBridgePlugin/status"

    print("URL:", url, flush=True)
    print("REQUEST: START", flush=True)

    response = requests.get(
        url,
        headers={"Authentication": password},
        timeout=10,
    )

    print("REQUEST: COMPLETE", flush=True)
    print("HTTP:", response.status_code, flush=True)
    print("RESPONSE LENGTH:", len(response.text), flush=True)
    print("RESPONSE:", response.text[:2000], flush=True)

except Exception as e:
    print("ERROR TYPE:", type(e).__name__, flush=True)
    print("ERROR:", str(e), flush=True)
    traceback.print_exc()
    raise
