import sqlite3
import os
import requests


DB_PATH = os.getenv(
    "DB_PATH",
    "/data/tradebot.db",
)

BASE_URL = "http://127.0.0.1:8000"

PROPOSAL_ID = (
    "cesarpereira27:14:945360-Ghosted:22584209635"
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


def main():
    print("=== 0.23.0 EXECUTOR CONTRACT ===")

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
        f"/execute/mock"
    )

    response = requests.post(
        url,
        timeout=30,
    )

    print("HTTP:", response.status_code)

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "MOCK_READY"

    # --------------------------------------------------------
    # EXECUTION CONTRACT
    # --------------------------------------------------------

    execution = data["execution"]
    operation = data["operation"]

    assert execution["enabled"] is True
    assert execution["allowed"] is True
    assert execution["executed"] is False
    assert execution["steam_write_operation"] is False
    assert execution["mode"] == "mock"

    assert operation["action"] == (
        "CREATE_MARKET_LISTING"
    )

    assert operation["market_hash_name"] == (
        "945360-Ghosted"
    )

    assert operation["market_name"] == "Ghosted"

    assert operation["asset_ids"] == [
        "22584209635"
    ]

    assert operation["quantity"] == 1
    assert operation["price"] == 0.07
    assert operation["currency"] == "EUR"

    # --------------------------------------------------------
    # SAFETY CONTRACT
    # --------------------------------------------------------

    safety = data["safety"]

    assert safety["proposal_approved"] is True
    assert safety["inventory_revalidated"] is True
    assert safety["already_executed"] is False
    assert safety["steam_write_operation"] is False

    # --------------------------------------------------------
    # MOCK RESPONSE
    # --------------------------------------------------------

    mock_response = data["mock_response"]

    assert mock_response["success"] is True
    assert mock_response["listing_created"] is False
    assert mock_response["external_listing_id"] is None

    # --------------------------------------------------------
    # DATABASE MUST NOT CHANGE
    # --------------------------------------------------------

    after = get_db_state()

    print("DB AFTER:", after)

    assert after == before

    print()
    print("=== 0.23.0 EXECUTOR CONTRACT: PASS ===")
    print("Execution contract: PASS")
    print("Safety contract: PASS")
    print("Database unchanged: PASS")
    print("Steam write: FALSE")


if __name__ == "__main__":
    main()
