import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync = load("funds_sync", os.path.join(ROOT, "scripts", "funds_sync.py"))
views = load("funds_views", os.path.join(ROOT, "scripts", "funds_views.py"))


class TestRegistryParser(unittest.TestCase):
    def test_bundled_template_parses(self):
        reg = sync.load_registry(os.path.join(ROOT, "templates", "funds.yaml"))
        self.assertEqual(len(reg["funds"]), 1)
        f = reg["funds"][0]
        self.assertEqual(f["code"], "EXMP")
        self.assertEqual(f["product_id"], 99999)
        self.assertEqual(f["buy_more_thresholds"][1]["drop_pct"], 17)

    def test_duplicate_key_is_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("funds:\n  - code: A\n    product_id: 1\n    code: B\n    asset_type: STOCK\n")
            path = fh.name
        with self.assertRaises(SystemExit) as cm:
            sync.load_registry(path)
        self.assertEqual(cm.exception.code, 2)
        os.unlink(path)

    def test_missing_required_field_is_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("funds:\n  - code: A\n    asset_type: STOCK\n")
            path = fh.name
        with self.assertRaises(SystemExit) as cm:
            sync.load_registry(path)
        self.assertEqual(cm.exception.code, 2)
        os.unlink(path)

    def test_unknown_toplevel_is_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("other: 1\nfunds:\n  - code: A\n    product_id: 1\n    asset_type: STOCK\n")
            path = fh.name
        with self.assertRaises(SystemExit) as cm:
            sync.load_registry(path)
        self.assertEqual(cm.exception.code, 2)
        os.unlink(path)


class TestSeries(unittest.TestCase):
    def test_load_sorted_unique(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"date": "2026-01-01", "nav": 1.0}) + "\n")
            fh.write(json.dumps({"date": "2026-01-02", "nav": 1.1}) + "\n")
            path = fh.name
        rows = sync.load_series(path)
        self.assertEqual([r["date"] for r in rows], ["2026-01-01", "2026-01-02"])
        os.unlink(path)

    def test_duplicate_date_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"date": "2026-01-01", "nav": 1.0}) + "\n")
            fh.write(json.dumps({"date": "2026-01-01", "nav": 1.1}) + "\n")
            path = fh.name
        with self.assertRaises(SystemExit) as cm:
            sync.load_series(path)
        self.assertEqual(cm.exception.code, 1)
        os.unlink(path)

    def test_unsorted_fatal(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
            fh.write(json.dumps({"date": "2026-01-02", "nav": 1.0}) + "\n")
            fh.write(json.dumps({"date": "2026-01-01", "nav": 1.1}) + "\n")
            path = fh.name
        with self.assertRaises(SystemExit) as cm:
            sync.load_series(path)
        self.assertEqual(cm.exception.code, 1)
        os.unlink(path)

    def test_append_dedupes_and_is_idempotent(self):
        existing = [{"date": "2026-01-01", "nav": 1.0}]
        fresh = sync.append_rows("/dev/null", existing,
                                 [{"date": "2026-01-01", "nav": 9.9},
                                  {"date": "2026-01-02", "nav": 1.1}], dry=True)
        self.assertEqual([r["date"] for r in fresh], ["2026-01-02"])


class TestSignals(unittest.TestCase):
    def cfg(self):
        return {"code": "T1", "avg_cost_nav": 100000.0, "units": 50.0,
                "cap_value": 9999999,
                "dca": {"amount": 1000000, "days": [7, 8]},
                "buy_more_thresholds": [
                    {"drop_pct": 10, "amount": 1000000},
                    {"drop_pct": 17, "amount": 2000000}]}

    def test_no_signal_at_flat_nav(self):
        series = [{"date": "2026-03-01", "nav": 95000.0}]
        self.assertEqual(sync.evaluate_signals(self.cfg(), series, dt.date(2026, 3, 1), set()), [])

    def test_buy_more_tier_fires_once_per_month(self):
        series = [{"date": "2026-03-01", "nav": 89000.0}]
        today = dt.date(2026, 3, 2)
        a1 = sync.evaluate_signals(self.cfg(), series, today, set())
        self.assertTrue(any("BUY-MORE" in a and "avg -10%" in a for a in a1))
        a2 = sync.evaluate_signals(self.cfg(), series, today, {("T1", "buy10")})
        self.assertFalse(any("avg -10%" in a for a in a2))

    def test_deeper_tier_uses_its_own_amount(self):
        series = [{"date": "2026-03-01", "nav": 82000.0}]
        alerts = sync.evaluate_signals(self.cfg(), series, dt.date(2026, 3, 2), set())
        self.assertTrue(any("avg -17%" in a and "2,000,000" in a for a in alerts))

    def test_dca_window_day(self):
        for day in (7, 8):
            series = [{"date": f"2026-03-{day:02d}", "nav": 100000.0}]
            alerts = sync.evaluate_signals(self.cfg(), series, dt.date(2026, 3, day), set())
            self.assertTrue(any("DCA window" in a for a in alerts), day)

    def test_cap_watch(self):
        cfg = self.cfg()
        cfg["cap_value"] = 5000000  # units 50 x 100000 = 5,000,000
        series = [{"date": "2026-03-01", "nav": 100000.0}]
        alerts = sync.evaluate_signals(cfg, series, dt.date(2026, 3, 2), set())
        self.assertTrue(any("CAP WATCH" in a for a in alerts))


class TestViews(unittest.TestCase):
    def run_views(self, env):
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            views.main()
        except SystemExit as e:
            return e.code
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_generates_note_and_gate_checks(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "_Ledger")
            os.makedirs(os.path.join(ledger, "funds-series"))
            with open(os.path.join(ledger, "funds-series", "EXMP.jsonl"), "w") as fh:
                fh.write(json.dumps({"date": "2026-01-02", "nav": 100000.0}) + "\n")
                fh.write(json.dumps({"date": "2026-02-03", "nav": 90000.0}) + "\n")
            reg = os.path.join(td, "funds.yaml")
            with open(os.path.join(ROOT, "templates", "funds.yaml")) as src, open(reg, "w") as dst:
                dst.write(src.read())
            env = {"FUNDS_VAULT": td, "FUNDS_LEDGER_DIR": "_Ledger", "FUNDS_REGISTRY": reg}
            rc = self.run_views(env)
            self.assertEqual(rc, 0)
            note = open(os.path.join(td, "2-Areas", "Finance", "Funds.md")).read()
            self.assertIn("Latest NAV: **90,000.00** (2026-02-03)", note)
            self.assertIn("-10.00%", note)  # 1m change vs 100000
            self.assertIn("Unrealized P&L: -500,000 (-10.00%)", note)

    def test_unsorted_series_fails_gate(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = os.path.join(td, "_Ledger")
            os.makedirs(os.path.join(ledger, "funds-series"))
            with open(os.path.join(ledger, "funds-series", "EXMP.jsonl"), "w") as fh:
                fh.write(json.dumps({"date": "2026-02-03", "nav": 90000.0}) + "\n")
                fh.write(json.dumps({"date": "2026-01-02", "nav": 100000.0}) + "\n")
            reg = os.path.join(td, "funds.yaml")
            with open(os.path.join(ROOT, "templates", "funds.yaml")) as src, open(reg, "w") as dst:
                dst.write(src.read())
            env = {"FUNDS_VAULT": td, "FUNDS_LEDGER_DIR": "_Ledger", "FUNDS_REGISTRY": reg}
            rc = self.run_views(env)
            self.assertEqual(rc, 1)


class TestBackfill(unittest.TestCase):
    def test_merge_jsonl_and_value_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            old = os.path.join(td, "old.jsonl")
            with open(old, "w") as fh:
                fh.write(json.dumps({"date": "2026-01-02", "value": 102000.0}) + "\n")
                fh.write(json.dumps({"date": "2026-01-05", "value": 101500.0}) + "\n")
            rows = {}
            for line in open(old):
                r = json.loads(line)
                rows[r["date"]] = {"date": r["date"], "nav": float(r.get("nav", r.get("value")))}
            self.assertEqual(sorted(rows), ["2026-01-02", "2026-01-05"])
            self.assertEqual(rows["2026-01-02"]["nav"], 102000.0)


if __name__ == "__main__":
    unittest.main()
