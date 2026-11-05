"""Puts the evidence folder on sys.path so that the tests can import the app package."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE = os.path.dirname(HERE)
REPO = os.path.dirname(EVIDENCE)
for path in (EVIDENCE,):
    if path not in sys.path:
        sys.path.insert(0, path)

TOPOLOGY_PATH = os.path.join(REPO, "docs", "topology.yaml")
SIGNALS_PATH = os.path.join(EVIDENCE, "config", "signals.json")
RANKING_PATH = os.path.join(EVIDENCE, "config", "ranking.json")
APP_DIR = os.path.join(EVIDENCE, "app")
