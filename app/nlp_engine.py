"""NLP engine factory for Presidio Analyzer.

Re-exports from pii_shield.nlp_engine for backward compatibility.
Loads .env for service-layer usage.
"""

from dotenv import load_dotenv

load_dotenv()  # reads .env from project root — service layer only

from pii_shield.nlp_engine import (  # noqa: E402, F401
    create_nlp_engine,
    get_nlp_engine_name,
)
