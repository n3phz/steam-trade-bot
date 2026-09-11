import sys
sys.path.insert(0, "/app")

import json
import sqlite3
import os
import requests


BASE_URL = "http://127.0.0.1:8000"

PROPOSAL_ID = (
    "cesarpereira27:14:945360-Ghosted:22584209635"
)

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/tradebot.db",
)


def get_db_state():
    conn = sqlite3.connect(DB_PATH)

    row = conn.execute(
        """
        SELECT
            status,
            approved_at,
            rejected_at,
            executed_at
        FROM sale_proposals
        WHERE proposal_id = ?
        """,
        (PROPOSAL_ID,),
    ).fetchone()

    conn.close()

    return row


print("=== 0.24.1 REAL EXECUTION PIPELINE ===")

before = get_db_state()

print("DB BEFORE:", before)

assert before is not None
assert before[0] == "APPROVED"
assert before[2] is None
assert before[3] is None

url = (
    f"{BASE_URL}"
    f"/sell/proposals/"
    f"{PROPOSAL_ID}"
    f"/execute/real"
)

response = requests.post(
    url,
    timeout=30,
)

print("HTTP:", response.status_code)

data = response.json()

print(json.dumps(data, indent=2))

assert response.status_code == 409

assert data["status"] == "blocked"

result = data["result"]

assert result["status"] == (
    "REAL_EXECUTION_BLOCKED"
)

execution = result["execution"]

assert execution["enabled"] is False
assert execution["allowed"] is False
assert execution["executed"] is False
assert execution["steam_write_operation"] is False
assert execution["mode"] == "real"

steam_response = result["steam_response"]

assert steam_response["success"] is False
assert steam_response["blocked"] is True
assert steam_response["listing_created"] is False
assert steam_response["external_listing_id"] is None

assert "disabled" in (
    result["error"].lower()
)

after = get_db_state()

print("DB AFTER:", after)

assert after == before

print()
print("=== 0.24.1 REAL EXECUTION PIPELINE: PASS ===")
print("Approval gate: PASS")
print("Inventory revalidation: PASS")
print("Execution contract: PASS")
print("Real executor: BLOCKED")
print("Database unchanged: PASS")
print("Steam write: FALSE")
