"""Example: Context-aware bank account number detection in PII Shield.

Demonstrates that PII Shield uses surrounding text context to correctly
distinguish bank account numbers from phone numbers, even when the digit
patterns overlap.

Scenarios covered:
  1. Bank account preceded by "bank account" context → US_BANK_NUMBER
  2. Phone number preceded by "mobile" / "call" context → PHONE_NUMBER
  3. Mixed text with both bank and phone numbers → each typed correctly
  4. Account number without context → falls below threshold (not detected)

Start the server first:  docker compose up -d
Then run:                python examples/bank_account_request.py
"""

import requests

BASE_URL = "http://localhost:8000"


def anonymize_unique(text: str, language: str = "en") -> dict:
    response = requests.post(
        f"{BASE_URL}/anonymize_unique",
        json={"text": text, "language": language},
    )
    response.raise_for_status()
    return response.json()


def print_result(title: str, text: str, result: dict) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")
    print(f"\n  Original:   {text}")
    print(f"  Anonymized: {result['anonymized_text']}\n")
    print("  Entity mapping:")
    for placeholder, original in result["entity_mapping"].items():
        print(f"    {placeholder:30s} → {original}")
    if result.get("hash_mapping"):
        print("  Hash mapping:")
        for h, original in result["hash_mapping"].items():
            print(f"    {h[:30]:30s} → {original}")


def check_entity(result: dict, value: str, expected_type: str) -> bool:
    """Verify a value was mapped to the expected entity type."""
    for placeholder, original in result["entity_mapping"].items():
        if original == value and expected_type in placeholder:
            return True
    return False


if __name__ == "__main__":
    passed = 0
    failed = 0

    # --- Scenario 1: Bank account with clear context ---
    text1 = (
        "Client Sneha Patil's bank account 917020056789012 at "
        "Contoso Bank, FC Road, Pune."
    )
    r1 = anonymize_unique(text1)
    print_result("Scenario 1: Bank account with context", text1, r1)

    if check_entity(r1, "917020056789012", "IN_BANK_ACCOUNT"):
        print("\n  ✅ PASS: 917020056789012 detected as IN_BANK_ACCOUNT")
        passed += 1
    else:
        print("\n  ❌ FAIL: 917020056789012 NOT detected as IN_BANK_ACCOUNT")
        failed += 1

    # --- Scenario 2: Phone number with phone context ---
    text2 = "Contact Rajesh on mobile 9823567890 for account queries."
    r2 = anonymize_unique(text2)
    print_result("Scenario 2: Phone number with phone context", text2, r2)

    if check_entity(r2, "9823567890", "PHONE_NUMBER"):
        print("\n  ✅ PASS: 9823567890 detected as PHONE_NUMBER")
        passed += 1
    else:
        print("\n  ❌ FAIL: 9823567890 NOT detected as PHONE_NUMBER")
        failed += 1

    # --- Scenario 3: Mixed — both bank and phone in same text ---
    text3 = (
        "Request to link demat account DP ID IN302201 with trading account "
        "for client Sneha Patil. Client's bank account 917020056789012 at "
        "Contoso Bank, FC Road, Pune. PAN: AEPPP5678Q. Contact details: "
        "sneha.patil@gmail.com, mobile 9823567890. Client DOB: 16/09/1993. "
        "Address: Flat 501, Kumar Pinnacle, Baner, Pune 411045. "
        "The customer also has a foreign (US) bank account: 12345678912 at Trey Research Bank"
    )
    r3 = anonymize_unique(text3)
    print_result("Scenario 3: Mixed bank + phone in same text", text3, r3)

    checks = [
        ("917020056789012", "IN_BANK_ACCOUNT", "15-digit Indian bank account"),
        ("12345678912", "US_BANK_NUMBER", "11-digit US bank account"),
        ("9823567890", "PHONE_NUMBER", "10-digit mobile number"),
    ]
    for value, expected, label in checks:
        if check_entity(r3, value, expected):
            print(f"\n  ✅ PASS: {value} ({label}) → {expected}")
            passed += 1
        else:
            print(f"\n  ❌ FAIL: {value} ({label}) NOT detected as {expected}")
            failed += 1

    # --- Scenario 4: Indian savings account at Woodgrove Bank ---
    text4 = "Transfer ₹50,000 to savings account 123456789012 at Woodgrove Bank."
    r4 = anonymize_unique(text4)
    print_result("Scenario 4: Indian savings account at Woodgrove Bank", text4, r4)

    if check_entity(r4, "123456789012", "IN_BANK_ACCOUNT"):
        print("\n  ✅ PASS: 123456789012 detected as IN_BANK_ACCOUNT")
        passed += 1
    else:
        print("\n  ❌ FAIL: 123456789012 NOT detected as IN_BANK_ACCOUNT")
        failed += 1

    # --- Scenario 5: US bank — Trey Research Bank ---
    text5 = "Wire to US bank account 0198765432101 at Trey Research Bank."
    r5 = anonymize_unique(text5)
    print_result("Scenario 5: US bank — Trey Research Bank", text5, r5)

    if check_entity(r5, "0198765432101", "US_BANK_NUMBER"):
        print("\n  ✅ PASS: 0198765432101 detected as US_BANK_NUMBER")
        passed += 1
    else:
        print("\n  ❌ FAIL: 0198765432101 NOT detected as US_BANK_NUMBER")
        failed += 1

    # --- Scenario 6: US bank — Margie's Travel Bank ---
    text6 = (
        "International wire transfer to account 123456789012 at "
        "Margie's Travel Bank, routing number 026009593."
    )
    r6 = anonymize_unique(text6)
    print_result("Scenario 6: US bank — Margie's Travel Bank", text6, r6)

    if check_entity(r6, "123456789012", "US_BANK_NUMBER"):
        print("\n  ✅ PASS: 123456789012 detected as US_BANK_NUMBER")
        passed += 1
    else:
        print("\n  ❌ FAIL: 123456789012 NOT detected as US_BANK_NUMBER")
        failed += 1

    # --- Summary ---
    total = passed + failed
    print(f"\n{'=' * 70}")
    print(f"  Results: {passed}/{total} passed, {failed}/{total} failed")
    print(f"{'=' * 70}\n")
