"""Example: Anonymize and restore Indian Driving License numbers via PII Shield.

Demonstrates the full round-trip:
  1. /anonymize_unique — detect PII and replace with unique placeholders
  2. /deanonymize      — restore the original DL number

Start the server first:  uvicorn app.main:app --reload
Then run:                python examples/driving_license_request.py
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


def deanonymize(session_id: str, text: str) -> dict:
    response = requests.post(
        f"{BASE_URL}/deanonymize",
        json={"id": session_id, "text": text},
    )
    response.raise_for_status()
    return response.json()


if __name__ == "__main__":
    sample_text = (
        "The applicant Rajesh Kumar holds driving license MH 14 2019 0012345 "
        "issued by the Pune RTO. His colleague Priya Sharma has license "
        "DL-05-2021-9876543 from Delhi."
    )

    print("=" * 60)
    print("  Indian Driving License — Anonymization Example")
    print("=" * 60)

    print("\nOriginal text:")
    print(f"  {sample_text}\n")

    # --- /anonymize_unique ---
    result = anonymize_unique(sample_text)

    print("Anonymized text:")
    print(f"  {result['anonymized_text']}\n")

    print("Entity mapping:")
    for placeholder, original in result["entity_mapping"].items():
        print(f"  {placeholder:30s} -> {original}")

    # --- /deanonymize ---
    deanon_result = deanonymize(
        session_id=result["id"],
        text=result["anonymized_text"],
    )

    print("\nDe-anonymized text:")
    print(f"  {deanon_result['text']}")
