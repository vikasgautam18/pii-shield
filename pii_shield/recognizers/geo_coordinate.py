"""Custom Presidio recognizer for geo-coordinates (latitude/longitude).

Detects geographic coordinate pairs and individual labeled coordinate
values in decimal degree (DD) and degrees-minutes-seconds (DMS) formats.

Coordinate ranges:
  - Latitude:  -90.0  to  +90.0
  - Longitude: -180.0 to +180.0

Context keywords such as *latitude*, *longitude*, *coordinates*, *GPS*,
etc. boost detection confidence to reduce false positives from bare
decimal numbers.
"""

from typing import Optional

import regex as re

from presidio_analyzer import Pattern, PatternRecognizer

# ── Decimal Degree (DD) Patterns ─────────────────────────────────────────

# Lat/lon pair: "28.6139, 77.2090" or "-33.8688, 151.2093"
_PATTERN_DD_PAIR = Pattern(
    name="geo_dd_pair",
    regex=(
        r"(?<!\d)"
        r"-?(?:1[0-8]\d|\d{1,2})"
        r"\.\d{2,7}"
        r"\s*[,;]\s*"
        r"-?(?:1[0-8]\d|\d{1,2})"
        r"\.\d{2,7}"
        r"(?!\d)"
    ),
    score=0.55,
)

# Single labeled coordinate: "latitude: 28.6139" or "lon: -77.2090"
_PATTERN_DD_LABELED = Pattern(
    name="geo_dd_labeled",
    regex=(
        r"(?i)"
        r"(?:lat(?:itude)?|lon(?:gitude)?|lng)"
        r"\s*[:=]\s*"
        r"-?(?:1[0-8]\d|\d{1,2})\.\d{2,7}"
    ),
    score=0.85,
)

# Coordinate with cardinal direction: "28.6139° N" or "28.6139N"
_PATTERN_DD_CARDINAL = Pattern(
    name="geo_dd_cardinal",
    regex=(
        r"(?<!\d)"
        r"-?(?:1[0-8]\d|\d{1,2})\.\d{2,7}"
        r"\s*°?\s*[NSEWnsew]"
        r"(?!\w)"
    ),
    score=0.75,
)

# ── Degrees-Minutes-Seconds (DMS) Patterns ───────────────────────────────

# DMS: 28°36'50"N or 28° 36' 50" N
_PATTERN_DMS = Pattern(
    name="geo_dms",
    regex=(
        r"\b\d{1,3}\s*°\s*\d{1,2}\s*[''′]\s*"
        r"(?:\d{1,2}(?:\.\d+)?\s*[\"″]\s*)?"
        r"[NSEWnsew]"
    ),
    score=0.85,
)

# ── Context Keywords ─────────────────────────────────────────────────────

_CONTEXT_WORDS = [
    "latitude",
    "longitude",
    "lat",
    "lon",
    "lng",
    "coordinates",
    "coordinate",
    "coords",
    "coord",
    "geo",
    "geolocation",
    "gps",
    "location",
    "position",
    "geocode",
    "latlng",
    "latlong",
    "point",
    "waypoint",
    "marker",
    "geojson",
    "wgs84",
    "map",
    "bearing",
    "degrees",
]


class GeoCoordinateRecognizer(PatternRecognizer):
    """Detects geographic coordinates (latitude/longitude) in text.

    Supports decimal degrees (DD), cardinal-direction notation, and
    degrees-minutes-seconds (DMS) formats.

    Uses ``validate_result`` to verify that matched values fall within
    valid coordinate ranges (lat: ±90, lon: ±180).
    """

    ENTITIES = ["GEO_COORDINATE"]

    def __init__(self) -> None:
        super().__init__(
            supported_entity="GEO_COORDINATE",
            supported_language="en",
            patterns=[
                _PATTERN_DMS,
                _PATTERN_DD_LABELED,
                _PATTERN_DD_CARDINAL,
                _PATTERN_DD_PAIR,
            ],
            context=_CONTEXT_WORDS,
        )

    def validate_result(self, pattern_text: str) -> Optional[bool]:
        """Validate that extracted numbers fall within valid coordinate ranges.

        Returns True if valid (score → 1.0), False if invalid (score → 0),
        or None to keep the pattern's base score.
        """
        numbers = re.findall(r"-?\d+(?:\.\d+)?", pattern_text)
        if not numbers:
            return None

        try:
            values = [float(n) for n in numbers]
        except ValueError:
            return None

        # For a pair, check lat/lon ranges
        if len(values) >= 2:
            lat, lon = values[0], values[1]
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return True
            return False

        # For a single value, accept if it could be either lat or lon
        if len(values) == 1:
            val = values[0]
            if -180 <= val <= 180:
                return None  # plausible but not certain
            return False

        return None
