"""Tests for the stockbiz.vn SSR series extractor and the sync overlap guard."""
import datetime
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

_spec = importlib.util.spec_from_file_location(
    "stockbiz_nav_series", os.path.join(ROOT, "scripts", "stockbiz_nav_series.py"))
assert _spec and _spec.loader
sb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sb)

# also load funds_sync for the overlap guard
_spec2 = importlib.util.spec_from_file_location(
    "funds_sync_mod", os.path.join(ROOT, "scripts", "funds_sync.py"))
assert _spec2 and _spec2.loader
fs = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(fs)

BS = chr(92)
Q = BS + BS + '"'  # real pages escape quotes as backslash-backslash-quote


def page(*arrays):
    """Build a fake stockbiz page: escaped flight-data arrays, M/D labels."""
    parts = []
    for arr in arrays:
        pts = ",".join(
            f"{{{Q}label{Q}:{Q}{lab}{Q},{Q}value{Q}:{val}}}" for lab, val in arr)
        parts.append(f"[{pts}]")
    return "<html>" + "xxxx".join(parts) + "</html>"


class TestResolveYears(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 26)

    def test_trailing_array_wraps_year(self):
        seg = [("12/30", "100"), ("12/31", "101"), ("1/2", "102"), ("10/23", "103")]
        got = dict(sb._resolve_years(seg, self.TODAY))
        self.assertEqual(got["2025-12-31"], 101.0)
        self.assertEqual(got["2026-01-02"], 102.0)
        self.assertEqual(got["2026-10-23"], 103.0)

    def test_same_label_two_years_both_kept(self):
        # chronological: Oct 2025 ... Dec 2025 ... Sep 2026 ... Oct 2026; the
        # Dec->Jan wrap is what lets the walk tell the two 10/23s apart
        seg = [("10/23", "108403.73"), ("12/31", "107127.08"),
               ("9/19", "94142.60"), ("10/23", "94142.61")]
        got = dict(sb._resolve_years(seg, self.TODAY))
        self.assertEqual(got["2025-10-23"], 108403.73)
        self.assertEqual(got["2025-12-31"], 107127.08)
        self.assertEqual(got["2026-09-19"], 94142.60)
        self.assertEqual(got["2026-10-23"], 94142.61)

    def test_feb29_kept_in_leap_year(self):
        today = datetime.date(2024, 3, 5)  # leap year: 2/29 exists
        seg = [("2/27", "97"), ("2/28", "98"), ("2/29", "99"), ("3/1", "100")]
        dates = {d for d, _ in sb._resolve_years(seg, today)}
        self.assertIn("2024-02-29", dates)


class TestExtractSeries(unittest.TestCase):
    def test_two_arrays_merged_clean(self):
        # 1Y array wrapping into 2025, then a current-month array whose labels
        # repeat - the break must not clobber the wrap-year rows
        # runs need >5 label hits to clear the aria-label guard
        arr1 = [("11/3", "103484.56"), ("11/4", "100868.72"), ("11/5", "103612.75"),
                ("11/6", "103110.15"), ("11/7", "102077.29"), ("12/31", "107127.08"),
                ("1/5", "108177.37"), ("9/19", "94142.60"), ("10/23", "94142.61")]
        arr2 = [("10/13", "92958.34"), ("10/14", "92565.47"), ("10/15", "92201.96"),
                ("10/16", "91756.26"), ("10/19", "92164.47"), ("10/20", "91571.50"),
                ("10/23", "94142.61")]
        got = dict(sb.extract_series(page(arr1, arr2)))
        self.assertEqual(got["2025-11-03"], 103484.56)
        self.assertEqual(got["2025-12-31"], 107127.08)
        self.assertEqual(got["2026-01-05"], 108177.37)
        self.assertEqual(got["2026-10-23"], 94142.61)
        self.assertEqual(got["2026-10-13"], 92958.34)

    def test_js_shell_detected(self):
        with self.assertRaises(SystemExit):
            sb.extract_series("<html>no data here</html>")

    def test_anchor_dates_land_on_trade_dates(self):
        """The 26/10 live-page shape: anchors must land on trade dates."""
        arr1 = [("10/31", "105718.25"), ("11/3", "103484.56"), ("11/4", "100868.72"),
                ("11/5", "103612.75"), ("11/6", "103110.15"), ("11/7", "102077.29"),
                ("12/31", "107127.08"), ("1/5", "108177.37"), ("10/23", "94142.61")]
        got = dict(sb.extract_series(page(arr1)))
        self.assertEqual(got.get("2025-10-31"), 105718.25)
        self.assertEqual(got.get("2026-10-23"), 94142.61)
        self.assertNotIn("2026-10-31", got)


class TestInferDailyRow(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 26)  # a Monday

    def test_accepts_cross_checked_nav(self):
        # tail Fri 10/23 = 94142.61; published 94486.43 = +0.37% (real 26/10 shape)
        latest = {"nav": 94486.43, "nav_to_previous": 0.37}
        row, why = fs.infer_daily_row(latest, {"date": "2026-10-23", "nav": 94142.61},
                                      self.TODAY)
        self.assertIsNotNone(row)
        self.assertEqual(row["date"], "2026-10-26")  # next weekday after Friday
        self.assertEqual(row["nav"], 94486.43)

    def test_friday_tail_rolls_to_monday(self):
        latest = {"nav": 95000.0, "nav_to_previous": (95000 / 94142.61 - 1) * 100}
        row, _ = fs.infer_daily_row(latest, {"date": "2026-10-23", "nav": 94142.61},
                                    self.TODAY)
        self.assertEqual(row["date"], "2026-10-26")

    def test_unchanged_nav_skipped(self):
        row, why = fs.infer_daily_row({"nav": 94142.61, "nav_to_previous": 0.0},
                                      {"date": "2026-10-23", "nav": 94142.61}, self.TODAY)
        self.assertIsNone(row)
        self.assertIn("unchanged", why)

    def test_mismatched_navtoprevious_rejected(self):
        # a missed session: stated change refers to a NAV we never saw
        latest = {"nav": 96000.0, "nav_to_previous": 1.23}
        row, why = fs.infer_daily_row(latest, {"date": "2026-10-23", "nav": 94142.61},
                                      self.TODAY)
        self.assertIsNone(row)
        self.assertIn("mismatch", why)

    def test_future_date_rejected(self):
        # tail IS today: next weekday would be in the future
        latest = {"nav": 95000.0, "nav_to_previous": (95000 / 94142.61 - 1) * 100}
        row, why = fs.infer_daily_row(latest, {"date": "2026-10-26", "nav": 94142.61},
                                      self.TODAY)
        self.assertIsNone(row)
        self.assertIn("future", why)


class TestReconcileHistory(unittest.TestCase):
    def test_history_wins_and_fills(self):
        existing = [{"date": "2026-10-23", "nav": 111.0},   # mis-inferred value
                    {"date": "2026-10-26", "nav": 94486.43}]
        hist = [{"date": "2026-10-23", "nav": 94142.61},    # authoritative
                {"date": "2026-10-24", "nav": 95000.0}]     # history-only date
        merged, corrected, added = fs.reconcile_history(
            "/nonexistent/path.jsonl", existing, hist, dry=True)
        got = {r["date"]: r["nav"] for r in merged}
        self.assertEqual(got["2026-10-23"], 94142.61)   # corrected
        self.assertEqual(got["2026-10-24"], 95000.0)    # added
        self.assertEqual(got["2026-10-26"], 94486.43)   # outside window: kept
        self.assertEqual((corrected, added), (1, 1))
        dates = [r["date"] for r in merged]
        self.assertEqual(dates, sorted(dates))


if __name__ == "__main__":
    unittest.main()
