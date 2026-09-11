import sys
sys.path.insert(0, "/app")

import main
import json

operation = {
    "action": "CREATE_MARKET_LISTING",
    "market_hash_name": "945360-Ghosted",
    "market_name": "Ghosted",
    "asset_ids": ["22584209635"],
    "quantity": 1,
    "price": 0.07,
    "currency": "EUR",
    "gross_value": 0.07,
    "estimated_net_value": 0.06,
}

print("=== 0.24.0 REAL EXECUTOR SAFETY ===")

result = main._execute_sale_contract_real(operation)

print(json.dumps(result, indent=2))

assert result["status"] == "REAL_EXECUTION_BLOCKED"

assert result["execution"]["enabled"] is False
assert result["execution"]["allowed"] is False
assert result["execution"]["executed"] is False
assert result["execution"]["steam_write_operation"] is False
assert result["execution"]["mode"] == "real"

assert result["steam_response"]["success"] is False
assert result["steam_response"]["blocked"] is True
assert result["steam_response"]["listing_created"] is False
assert result["steam_response"]["external_listing_id"] is None

assert "disabled" in result["error"].lower()

print()
print("=== 0.24.0 REAL EXECUTOR SAFETY: PASS ===")
print("Real executor: BLOCKED")
print("Steam write: FALSE")
print("Listing created: FALSE")
