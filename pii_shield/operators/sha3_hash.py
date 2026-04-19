"""Custom Presidio operator that hashes text using SHA3-256."""

import hashlib
from typing import Dict

from presidio_anonymizer.operators import Operator, OperatorType


class Sha3HashOperator(Operator):
    """Hash text using SHA3-256 and return the hex digest."""

    def operate(self, text: str = None, params: Dict = None) -> str:
        return hashlib.sha3_256(text.encode()).hexdigest()

    def validate(self, params: Dict = None) -> None:
        pass

    def operator_name(self) -> str:
        return "sha3_hash"

    def operator_type(self) -> OperatorType:
        return OperatorType.Anonymize
