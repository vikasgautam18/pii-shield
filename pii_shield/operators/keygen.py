"""Generate an ML-KEM-768 key pair for PQC encryption.

Usage:
    python -m app.operators.keygen

Prints environment variable lines ready to paste into .env:
    PQC_ENCAPSULATION_KEY=...
    PQC_DECAPSULATION_KEY=...
"""

import base64

from mlkem import ML_KEM, MLKEM_768_PARAMETERS


def main() -> None:
    ml_kem = ML_KEM(MLKEM_768_PARAMETERS)
    ek, dk = ml_kem.key_gen()

    ek_b64 = base64.b64encode(bytes(ek)).decode()
    dk_b64 = base64.b64encode(bytes(dk)).decode()

    print("# ML-KEM-768 key pair — paste into your .env file")
    print(f"PQC_ENCAPSULATION_KEY={ek_b64}")
    print(f"PQC_DECAPSULATION_KEY={dk_b64}")
    print()
    print(f"# Encapsulation key size: {len(bytes(ek))} bytes")
    print(f"# Decapsulation key size: {len(bytes(dk))} bytes")


if __name__ == "__main__":
    main()
