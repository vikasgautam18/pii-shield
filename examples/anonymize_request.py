"""Example: Call the PII Shield /anonymize_unique and /deanonymize endpoints."""

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
        "My name is John Smith and my email is john.smith@example.com. "
        "I live at 123 Main Street, New York. My phone number is 212-555-1234."
    )

    print("Original text:")
    print(f"  {sample_text}\n")

    # --- /anonymize_unique ---
    unique_result = anonymize_unique(sample_text)

    print("Anonymized text (unique IDs):")
    print(f"  {unique_result['anonymized_text']}\n")

    print("Entity mapping:")
    for placeholder, original in unique_result["entity_mapping"].items():
        print(f"  {placeholder} -> {original}")

    # --- /deanonymize ---
    deanon_result = deanonymize(
        session_id=unique_result["id"],
        text=unique_result["anonymized_text"],
    )

    print("\nDe-anonymized text:")
    print(f"  {deanon_result['text']}")
