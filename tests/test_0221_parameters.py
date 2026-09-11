import sqlite3
import os
import json
import requests
from datetime import datetime, timezone


DB_PATH = os.getenv("DB_PATH", "/data/tradebot.db")
BASE_URL = "http://127.0.0.1:8000"

TESTS = [
    (
        "sandbox-test-zero-qty",
        0,
        0.07,
        "SELL_DUPLICATE",
    ),
    (
        "sandbox-test-zero-price",
        1,
        0.00,
        "SELL_DUPLICATE",
    ),
    (
        "sandbox-test-classification",
        1,
        0.07,
        "KEEP",
    ),
]


def create_test_proposals():
    conn = sqlite3.connect(DB_PATH)

    now = datetime.now(timezone.utc).isoformat()

    for proposal_id, quantity, price, classification in TESTS:
        conn.execute(
            """
            INSERT OR REPLACE INTO sale_proposals (
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
                approved_at
            )
            VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            (
                proposal_id,
                "sandbox-test",
                14,
                "945360-Ghosted",
                "Ghosted",
                json.dumps(["22584209635"]),
                quantity,
                price,
                0,
                0,
                "SELL",
                classification,
                "HIGH",
                60,
                "FRESH",
                "APPROVED",
                "0.22.1 parameter safety test.",
                now,
                now,
            ),
        )

    conn.commit()
    conn.close()


def cleanup():
    conn = sqlite3.connect(DB_PATH)

    conn.execute(
        """
        DELETE FROM sale_proposals
        WHERE proposal_id LIKE 'sandbox-test-%'
        """
    )

    conn.commit()

    remaining = conn.execute(
        """
        SELECT COUNT(*)
        FROM sale_proposals
        WHERE proposal_id LIKE 'sandbox-test-%'
        """
    ).fetchone()[0]

    conn.close()

    assert remaining == 0


def run_tests():
    print("=== 0.22.1 PARAMETER SAFETY ===")

    for proposal_id, _, _, _ in TESTS:
        url = (
            f"{BASE_URL}"
            f"/sell/proposals/"
            f"{proposal_id}"
            f"/execute/mock"
        )

        response = requests.post(
            url,
            timeout=30,
        )

        print()
        print("TEST:", proposal_id)
        print("HTTP:", response.status_code)

        assert response.status_code == 409

        data = response.json()

        execution = (
            data.get("execution")
            or data.get("result", {}).get("execution")
            or {}
        )

        print(
            "STEAM_WRITE:",
            execution.get(
                "steam_write_operation"
            ),
        )

        print(
            "EXECUTED:",
            execution.get("executed"),
        )

        assert (
            execution.get(
                "steam_write_operation"
            )
            is False
        )

        assert (
            execution.get("executed")
            is False
        )

        print("PASS")


if __name__ == "__main__":
    try:
        create_test_proposals()
        run_tests()
        print()
        print(
            "=== 0.22.1 PARAMETER SAFETY: PASS ==="
        )
    finally:
        cleanup()
        print(
            "=== TEST DATA CLEANUP: PASS ==="
        )
