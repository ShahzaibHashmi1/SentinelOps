"""Puts detector/ on sys.path so that tests can `from app import ...` (run from the repository root:
python -m unittest discover -s detector/tests)."""
import sys
from pathlib import Path

DETECTOR_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = DETECTOR_DIR.parent
RULES_JSON = DETECTOR_DIR / "rules" / "rules.json"
CATALOGUE_JSON = REPO_DIR / "chaos" / "catalogue.json"
if str(DETECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(DETECTOR_DIR))
