import sqlite3
import os
import requests
import json


DB_PATH = os.getenv("DB_PATH", "/data/tradebot.db")

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


def execute_mock():
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

    print(
        json.dumps(
            data,
            indent=2,
        )
    )

    return data


def main():
    print("=== 0.22.2 MOCK IDEMPOTENCY ===")

    before = get_db_state()

    print()
    print("DB BEFORE:", before)

    assert before is not None
    assert before[0] == "APPROVED"
    assert before[2] is None
    assert before[3] is None

    print()
    print("=== FIRST MOCK EXECUTION ===")

    first = execute_mock()

    assert first["status"] == "MOCK_READY"

    assert (
        first["execution"]["mode"]
        == "mock"
    )

    assert (
        first["execution"]["executed"]
        is False
    )

    assert (
        first["execution"]["steam_write_operation"]
        is False
    )

    assert (
        first["mock_response"]["success"]
        is True
    )

    assert (
        first["mock_response"]["listing_created"]
        is False
    )

    assert (
        first["mock_response"]["external_listing_id"]
        is None
    )

    execution_id_1 = first["execution_id"]

    after_first = get_db_state()

    print()
    print("DB AFTER FIRST:", after_first)

    assert after_first == before

    print()
    print("=== SECOND MOCK EXECUTION ===")

    second = execute_mock()

    assert second["status"] == "MOCK_READY"

    assert (
        second["execution"]["executed"]
        is False
    )

    assert (
        second["execution"]["steam_write_operation"]
        is False
    )

    assert (
        second["mock_response"]["listing_created"]
        is False
    )

    assert (
        second["mock_response"]["external_listing_id"]
        is None
    )

    execution_id_2 = second["execution_id"]

    after_second = get_db_state()

    print()
    print("DB AFTER SECOND:", after_second)

    assert after_second == before

    print()
    print("EXECUTION ID 1:", execution_id_1)
    print("EXECUTION ID 2:", execution_id_2)

    assert execution_id_1 == execution_id_2

    print()
    print("=== 0.22.2 IDEMPOTENCY: PASS ===")
    print("Same execution ID: PASS")
    print("DB unchanged: PASS")
    print("Steam write: FALSE")
    print("Executed: FALSE")


if __name__ == "__main__":
    main()
