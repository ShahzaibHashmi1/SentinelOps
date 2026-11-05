"""Static safety rules for evidence/app (the in-process scan of D68 and DoD 9).

- no psycopg, redis, requests or subprocess anywhere in evidence/app/*.py (the words themselves are scanned,
  comments and strings included; service names and PromQL live in the config files)
- the container runtime word appears only in the one module that talks to the runtime (docker_source.py,
  built in CP2); the fault-injection marker appears only in redact.py and that module
- imports are the standard library plus networkx
"""
import ast
import glob
import os
import re
import sys
import unittest

import _bootstrap  # noqa: F401
from _bootstrap import APP_DIR

FORBIDDEN = re.compile(r"psycopg|redis|requests|subprocess", re.IGNORECASE)
RUNTIME_WORD = re.compile(r"docker", re.IGNORECASE)
MARKER_WORD = re.compile(r"chaos", re.IGNORECASE)
POSITIVE = re.compile(r"sqlite3|networkx|urllib\.request|http\.server|threading", re.IGNORECASE)
RUNTIME_MODULE = "docker_source.py"
MARKER_MODULES = {"redact.py", RUNTIME_MODULE}
ALLOWED_THIRD_PARTY = {"networkx"}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def app_files():
    return sorted(glob.glob(os.path.join(APP_DIR, "*.py")))


class ScanTests(unittest.TestCase):
    def test_there_are_files_to_scan(self):
        self.assertGreaterEqual(len(app_files()), 10)

    def test_forbidden_words_do_not_appear(self):
        for path in app_files():
            text = read(path)
            self.assertIsNone(FORBIDDEN.search(text), f"{os.path.basename(path)}: {FORBIDDEN.search(text)}")

    def test_runtime_word_only_in_its_own_module(self):
        for path in app_files():
            name = os.path.basename(path)
            text = read(path)
            if name != RUNTIME_MODULE:
                self.assertIsNone(RUNTIME_WORD.search(text), name)

    def test_marker_word_only_where_the_filter_lives(self):
        for path in app_files():
            name = os.path.basename(path)
            text = read(path)
            if name not in MARKER_MODULES:
                self.assertIsNone(MARKER_WORD.search(text), name)

    def test_positive_control(self):
        """The scan would notice a violation: the patterns match code that is really there."""
        text = "\n".join(read(p) for p in app_files())
        for word in ("sqlite3", "networkx", "threading"):
            self.assertIsNotNone(re.search(word, text), word)
        self.assertIsNotNone(FORBIDDEN.search("import subprocess"))
        self.assertIsNotNone(FORBIDDEN.search("Redis"))
        self.assertIsNotNone(RUNTIME_WORD.search("DOCKER_HOST"))
        self.assertIsNotNone(MARKER_WORD.search("chaos-F11"))

    def test_imports_are_standard_library_or_networkx(self):
        stdlib = set(sys.stdlib_module_names)
        for path in app_files():
            tree = ast.parse(read(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:  # relative import inside the package
                        continue
                    modules = [(node.module or "").split(".")[0]]
                else:
                    continue
                for module in modules:
                    self.assertTrue(module in stdlib or module in ALLOWED_THIRD_PARTY, f"{os.path.basename(path)} imports {module}")

    def test_no_dynamic_code_or_shell(self):
        for path in app_files():
            text = read(path)
            for needle in ("os.system", "eval(", "exec(", "__import__", "pickle"):
                self.assertNotIn(needle, text, f"{os.path.basename(path)}: {needle}")

    def test_no_file_reads_outside_config_and_topology(self):
        """Modules that read files take the path as an argument; none opens a path by itself."""
        for path in app_files():
            text = read(path)
            self.assertNotIn("/var/run", text)
            self.assertNotIn(".env", text.replace("environment", ""))


if __name__ == "__main__":
    unittest.main()
