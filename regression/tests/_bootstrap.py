"""Import path of the regression tests (same role as the _bootstrap.py of chaos/tests and detector/tests).

scripts/regression.py is loaded from its file under the module name `regression_runner`, so that the folder
`regression/` of the repository root can never be mistaken for it. The module is registered in sys.modules before it runs
(dataclasses need that)."""
import importlib.util
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_DIR / "scripts"
RUNNER_PATH = SCRIPTS_DIR / "regression.py"
LEDGER_PATH = REPO_DIR / "docs" / "REGRESSION.md"


def load_runner():
    name = "regression_runner"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = load_runner()
