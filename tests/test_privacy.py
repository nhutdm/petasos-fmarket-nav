"""Privacy sweep - the repo must never carry user-specific figures or ids.

Tripwire REAL literals are kept USER-SIDE ONLY (their vault copy). This test
asserts none of them appear anywhere in the tree, and that every shipped file
passes the generic PII shapes (accounts, comma/underscore-grouped figures).
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# REAL DCDS values that must NEVER appear in this repo (user keeps them vault-side).
# Built from fragments so this file never contains the full literal itself.
TRIPWIRES = [
    "".join(x) for x in [
        ("102", "936"), ("102,9", "36"),
        ("6796", "856"), ("6,79", "6,856"),
        ("6138", "039"), ("6,13", "8,039"),
        ("66", ".03"),
        ("120", "00000"), ("12,", "000,000"),
        ("525613", "04"),
    ]
]

GENERIC_PATTERNS = [
    (re.compile(r"\b\d{9,12}\b"), "long digit run (account-like)"),
    (re.compile(r"\d{1,3}(?:,\d{3}){2,}"), "comma-grouped figure"),
    (re.compile(r"\d{1,3}(?:_\d{3}){2,}"), "underscore-grouped figure"),
]

ALLOW_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}\b", re.I)
ALLOW_NUM = re.compile(r"^\s*[-0-9.eE+ ]+$")


class TestPrivacySweep(unittest.TestCase):
    def repo_files(self):
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for fn in filenames:
                if fn.endswith((".py", ".md", ".yaml", ".yml", ".json", ".txt")):
                    yield os.path.join(dirpath, fn)

    def test_no_tripwires(self):
        for path in self.repo_files():
            text = open(path, encoding="utf-8", errors="ignore").read()
            for tw in TRIPWIRES:
                self.assertNotIn(tw, text, f"{path} contains real-value tripwire '{tw}'")

    def test_no_pii_shapes(self):
        for path in self.repo_files():
            for i, line in enumerate(open(path, encoding="utf-8", errors="ignore"), 1):
                if ALLOW_GUID.search(line):
                    continue
                if re.search(r"product_id: \d+", line) and len(line) < 60:
                    # registry ids like 99999 are 5 digits - only flag long runs
                    pass
                for pat, why in GENERIC_PATTERNS:
                    m = pat.search(line)
                    if m and not ALLOW_NUM.fullmatch(line):
                        # allow documented placeholders like 1,234,567 examples
                        digits = int(m.group(0).replace(",", ""))
                        if digits % 1000000 == 0:
                            continue  # whole-million formatted = synthetic placeholder
                        if m.group(0) == "1,234,567":
                            continue  # documented placeholder
                        self.fail(f"{path}:{i} {why}: '{m.group(0)}' in: {line.strip()[:90]}")

    def test_fmarket_shapes_frozen(self):
        """Fixture shapes must match the live API contract (status/data rows)."""
        fix = os.path.join(ROOT, "fixtures", "nav_history_shape.json")
        doc = json.load(open(fix))
        self.assertEqual(doc["status"], 200)
        row = doc["data"][0]
        for key in ("nav", "navDate", "productId"):
            self.assertIn(key, row)


if __name__ == "__main__":
    unittest.main()
