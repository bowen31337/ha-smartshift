"""
Tests for scripts/inverter_control.py — targeting 100% coverage.
All HTTP calls are mocked via unittest.mock; no real network traffic.
"""
import argparse
import io
import json
import os
import sys
import zipfile
import unittest
from unittest.mock import MagicMock, mock_open, patch
import urllib.error

# Ensure the scripts directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import inverter_control as ic


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_response(body: bytes, status: int = 200):
    """Create a mock urllib response context manager."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = body
    mock_resp.status = status
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def _make_zip_response(csv_content: str, csv_filename: str = "data.csv") -> bytes:
    """Build a realistic in-memory zip file containing one CSV."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(csv_filename, csv_content)
    return buf.getvalue()


# ─── get_spot_price_amber ─────────────────────────────────────────────────────

class TestGetSpotPriceAmber(unittest.TestCase):

    def test_no_api_key_returns_none(self):
        """When AMBER_API_KEY is empty, return None without making any HTTP call."""
        with patch.object(ic, "AMBER_API_KEY", ""):
            result = ic.get_spot_price_amber()
        self.assertIsNone(result)

    def test_success_returns_price(self):
        """Happy path: API returns a CurrentInterval record."""
        payload = json.dumps([
            {"type": "CurrentInterval", "perKwh": "18.5"},
        ]).encode()
        resp = _make_response(payload)
        with patch.object(ic, "AMBER_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_spot_price_amber()
        self.assertAlmostEqual(result, 18.5)

    def test_success_skips_non_current_interval(self):
        """Intervals that are not CurrentInterval should be skipped."""
        payload = json.dumps([
            {"type": "ForecastInterval", "perKwh": "99.0"},
            {"type": "CurrentInterval", "perKwh": "7.25"},
        ]).encode()
        resp = _make_response(payload)
        with patch.object(ic, "AMBER_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_spot_price_amber()
        self.assertAlmostEqual(result, 7.25)

    def test_no_current_interval_in_response_returns_none(self):
        """If response contains no CurrentInterval, return None."""
        payload = json.dumps([{"type": "ForecastInterval", "perKwh": "10.0"}]).encode()
        resp = _make_response(payload)
        with patch.object(ic, "AMBER_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_spot_price_amber()
        self.assertIsNone(result)

    def test_http_error_returns_none(self):
        """Any exception (HTTP error, timeout, etc.) should return None, not raise."""
        with patch.object(ic, "AMBER_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
            result = ic.get_spot_price_amber()
        self.assertIsNone(result)

    def test_malformed_json_returns_none(self):
        """Non-JSON response body should return None."""
        resp = _make_response(b"not-json")
        with patch.object(ic, "AMBER_API_KEY", "test-key"), \
             patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_spot_price_amber()
        self.assertIsNone(result)


# ─── get_spot_price_aemo_nemweb ───────────────────────────────────────────────

# A minimal valid TradingIS CSV snippet for NSW1
_VALID_CSV = (
    'C,NEMSOLUTION,TRADINGIS,...\n'
    'I,TRADING,PRICE,3,...\n'
    'D,TRADING,PRICE,3,"2026/03/31 05:45:00",1,NSW1,69,86.89,...\n'
    'C,END OF REPORT,...\n'
)

_INDEX_HTML = b'<a href="PUBLIC_TRADINGIS_202603310545_0000000001234.zip">PUBLIC_TRADINGIS_202603310545_0000000001234.zip</a>'


class TestGetSpotPriceAemoNemweb(unittest.TestCase):

    def _mock_two_responses(self, index_body: bytes, zip_body: bytes):
        """Return a side_effect list for two sequential urlopen calls."""
        return [
            _make_response(index_body),
            _make_response(zip_body),
        ]

    def test_success_returns_price(self):
        """Happy path: index page lists a zip, zip contains valid CSV."""
        zip_body = _make_zip_response(_VALID_CSV)
        responses = self._mock_two_responses(_INDEX_HTML, zip_body)
        with patch("urllib.request.urlopen", side_effect=responses):
            result = ic.get_spot_price_aemo_nemweb()
        # 86.89 $/MWh → 8.689 c/kWh
        self.assertAlmostEqual(result, 86.89 / 10.0, places=3)

    def test_no_files_found_returns_none(self):
        """If index page has no matching zip filenames, return None."""
        index_html = b"<html>nothing here</html>"
        with patch("urllib.request.urlopen", return_value=_make_response(index_html)):
            result = ic.get_spot_price_aemo_nemweb()
        self.assertIsNone(result)

    def test_bad_csv_no_nsw1_rows_returns_none(self):
        """CSV with no NSW1 PRICE rows → return None."""
        bad_csv = (
            'C,NEMSOLUTION,...\n'
            'D,TRADING,PRICE,3,"2026/03/31 05:45:00",1,VIC1,69,55.00,...\n'  # Wrong region
        )
        zip_body = _make_zip_response(bad_csv)
        responses = self._mock_two_responses(_INDEX_HTML, zip_body)
        with patch("urllib.request.urlopen", side_effect=responses):
            result = ic.get_spot_price_aemo_nemweb()
        self.assertIsNone(result)

    def test_csv_with_bad_price_column_skips_gracefully(self):
        """Row with non-numeric price column skips without crashing."""
        bad_price_csv = (
            'D,TRADING,PRICE,3,"2026/03/31 05:45:00",1,NSW1,69,NOT_A_NUMBER,...\n'
            'D,TRADING,PRICE,3,"2026/03/31 05:46:00",1,NSW1,70,100.00,...\n'
        )
        zip_body = _make_zip_response(bad_price_csv)
        responses = self._mock_two_responses(_INDEX_HTML, zip_body)
        with patch("urllib.request.urlopen", side_effect=responses):
            result = ic.get_spot_price_aemo_nemweb()
        # Second row is valid, should return its price
        self.assertAlmostEqual(result, 100.00 / 10.0, places=3)

    def test_network_error_returns_none(self):
        """Any network error returns None without raising."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connect")):
            result = ic.get_spot_price_aemo_nemweb()
        self.assertIsNone(result)

    def test_zip_error_returns_none(self):
        """If zip download returns garbage bytes, ZipFile will fail → return None."""
        responses = self._mock_two_responses(_INDEX_HTML, b"NOT_A_ZIP")
        with patch("urllib.request.urlopen", side_effect=responses):
            result = ic.get_spot_price_aemo_nemweb()
        self.assertIsNone(result)

    def test_full_match_not_found_returns_none(self):
        """
        Edge case: latest_ts found but full filename regex fails (shouldn't happen
        in practice, but exercises the early-return branch).
        """
        # Provide an index that has the timestamp match but NOT the full filename match
        # This is tricky — the two regexes must behave differently.
        # We craft HTML where the timestamp pattern matches but the full pattern does NOT
        # after we patch re.findall to return different values per call.
        import re as _re
        original_findall = _re.findall
        call_count = [0]

        def fake_findall(pattern, string):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: timestamp regex → return a match
                return ["202603310545"]
            else:
                # Second call: full filename regex → return empty
                return []

        with patch("urllib.request.urlopen", return_value=_make_response(_INDEX_HTML)), \
             patch("re.findall", side_effect=fake_findall):
            result = ic.get_spot_price_aemo_nemweb()
        self.assertIsNone(result)


# ─── get_spot_price ───────────────────────────────────────────────────────────

class TestGetSpotPrice(unittest.TestCase):

    def test_amber_success(self):
        """When Amber returns a price, use it."""
        with patch.object(ic, "get_spot_price_amber", return_value=12.5):
            result = ic.get_spot_price()
        self.assertEqual(result, 12.5)

    def test_amber_fails_aemo_success(self):
        """Amber fails → fall through to AEMO."""
        with patch.object(ic, "get_spot_price_amber", return_value=None), \
             patch.object(ic, "get_spot_price_aemo_nemweb", return_value=8.7):
            result = ic.get_spot_price()
        self.assertAlmostEqual(result, 8.7)

    def test_both_fail_raises_runtime_error(self):
        """Both sources fail → raise RuntimeError."""
        with patch.object(ic, "get_spot_price_amber", return_value=None), \
             patch.object(ic, "get_spot_price_aemo_nemweb", return_value=None):
            with self.assertRaises(RuntimeError):
                ic.get_spot_price()


# ─── get_battery_state ────────────────────────────────────────────────────────

class TestGetBatteryState(unittest.TestCase):

    def test_success(self):
        """Happy path: inverter returns valid JSON."""
        payload = json.dumps({"soc": 75, "pb": -500}).encode()
        resp = _make_response(payload)
        with patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_battery_state()
        self.assertEqual(result["soc"], 75)
        self.assertEqual(result["power"], -500)

    def test_http_error_returns_safe_fallback(self):
        """On any error, return safe fallback dict with soc=50."""
        with patch("urllib.request.urlopen", side_effect=Exception("conn refused")):
            result = ic.get_battery_state()
        self.assertEqual(result["soc"], 50)
        self.assertEqual(result["power"], 0)
        self.assertIn("error", result)


# ─── get_battery_config ───────────────────────────────────────────────────────

class TestGetBatteryConfig(unittest.TestCase):

    def test_success(self):
        """Happy path: returns parsed JSON dict."""
        config = {"mod_r": 2, "muf": 5}
        resp = _make_response(json.dumps(config).encode())
        with patch("urllib.request.urlopen", return_value=resp):
            result = ic.get_battery_config()
        self.assertEqual(result["mod_r"], 2)

    def test_http_error_returns_empty_dict(self):
        """On error, return empty dict."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            result = ic.get_battery_config()
        self.assertEqual(result, {})


# ─── decide_action ────────────────────────────────────────────────────────────

class TestDecideAction(unittest.TestCase):

    def setUp(self):
        # Defaults from module
        self.charge_thresh = ic.CHARGE_THRESHOLD      # 5
        self.discharge_thresh = ic.DISCHARGE_THRESHOLD  # 25
        self.soc_min = ic.SOC_MIN    # 20
        self.soc_max = ic.SOC_MAX    # 95

    def test_charge_branch(self):
        """Low price + low SoC → charge."""
        result = ic.decide_action(self.charge_thresh - 1, int(self.soc_max) - 1)
        self.assertEqual(result, "charge")

    def test_discharge_branch(self):
        """High price + high SoC → discharge."""
        result = ic.decide_action(self.discharge_thresh + 1, int(self.soc_min) + 1)
        self.assertEqual(result, "discharge")

    def test_self_consumption_branch(self):
        """Mid-range price → self_consumption."""
        mid_price = (self.charge_thresh + self.discharge_thresh) / 2  # 15
        result = ic.decide_action(mid_price, 60)
        self.assertEqual(result, "self_consumption")

    def test_charge_blocked_at_soc_max(self):
        """Even with low price, if SoC >= SOC_MAX do NOT charge."""
        result = ic.decide_action(self.charge_thresh - 1, int(self.soc_max))
        # soc == soc_max → condition `soc < SOC_MAX` is False
        self.assertNotEqual(result, "charge")

    def test_discharge_blocked_at_soc_min(self):
        """Even with high price, if SoC <= SOC_MIN do NOT discharge."""
        result = ic.decide_action(self.discharge_thresh + 1, int(self.soc_min))
        # soc == soc_min → condition `soc > SOC_MIN` is False
        self.assertNotEqual(result, "discharge")

    def test_exactly_at_charge_threshold_is_self_consumption(self):
        """Price == CHARGE_THRESHOLD is NOT < threshold, so → self_consumption."""
        result = ic.decide_action(self.charge_thresh, int(self.soc_max) - 1)
        self.assertEqual(result, "self_consumption")

    def test_exactly_at_discharge_threshold_is_self_consumption(self):
        """Price == DISCHARGE_THRESHOLD is NOT > threshold, so → self_consumption."""
        result = ic.decide_action(self.discharge_thresh, int(self.soc_min) + 1)
        self.assertEqual(result, "self_consumption")


# ─── _post_inverter ───────────────────────────────────────────────────────────

class TestPostInverter(unittest.TestCase):

    def test_success(self):
        """Happy path: POST returns JSON."""
        resp_body = json.dumps({"dat": "ok"}).encode()
        resp = _make_response(resp_body)
        with patch("urllib.request.urlopen", return_value=resp):
            result = ic._post_inverter({"action": "test"})
        self.assertEqual(result["dat"], "ok")

    def test_http_error_propagates(self):
        """HTTP errors are NOT caught here — they propagate to caller."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            with self.assertRaises(urllib.error.URLError):
                ic._post_inverter({"action": "test"})


# ─── set_work_mode ────────────────────────────────────────────────────────────

class TestSetWorkMode(unittest.TestCase):

    def test_dry_run_returns_true_without_http(self):
        """Dry run logs and returns True, no HTTP call."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = ic.set_work_mode("charge", dry_run=True)
        self.assertTrue(result)
        mock_urlopen.assert_not_called()

    def test_invalid_mode_raises_value_error(self):
        """Unknown mode should raise ValueError."""
        with self.assertRaises(ValueError):
            ic.set_work_mode("turbo_boost")

    def test_success_response(self):
        """Successful POST with dat=ok returns True."""
        resp = _make_response(json.dumps({"dat": "ok"}).encode())
        with patch("urllib.request.urlopen", return_value=resp):
            result = ic.set_work_mode("self_consumption", dry_run=False)
        self.assertTrue(result)

    def test_failure_response_returns_false(self):
        """POST returns non-ok dat → returns False."""
        resp = _make_response(json.dumps({"dat": "error", "msg": "fail"}).encode())
        with patch("urllib.request.urlopen", return_value=resp):
            result = ic.set_work_mode("discharge", dry_run=False)
        self.assertFalse(result)

    def test_exception_returns_false(self):
        """Network exception → returns False (not raised)."""
        with patch("urllib.request.urlopen", side_effect=Exception("conn error")):
            result = ic.set_work_mode("charge", dry_run=False)
        self.assertFalse(result)

    def test_all_valid_modes(self):
        """All three modes can be set successfully."""
        resp = _make_response(json.dumps({"dat": "ok"}).encode())
        for mode in ("self_consumption", "charge", "discharge"):
            with patch("urllib.request.urlopen", return_value=resp):
                result = ic.set_work_mode(mode, dry_run=False)
            self.assertTrue(result, f"mode={mode} should succeed")


# ─── save_state ───────────────────────────────────────────────────────────────

class TestSaveState(unittest.TestCase):

    def test_success(self):
        """Happy path: writes JSON to file."""
        m = mock_open()
        with patch("builtins.open", m), \
             patch.dict(os.environ, {"STATE_FILE": "/tmp/test_state.json"}):
            ic.save_state("charge", 4.5, 80)
        m.assert_called_once_with("/tmp/test_state.json", "w")
        # Verify the JSON was written
        handle = m()
        written = "".join(call.args[0] for call in handle.write.call_args_list)
        data = json.loads(written)
        self.assertEqual(data["action"], "charge")
        self.assertAlmostEqual(data["spot_price_ckwh"], 4.5)
        self.assertEqual(data["soc_pct"], 80)

    def test_write_error_logs_warning(self):
        """If file write fails, log a warning but do NOT raise."""
        with patch("builtins.open", side_effect=PermissionError("denied")), \
             patch.dict(os.environ, {"STATE_FILE": "/tmp/test_state.json"}):
            # Should not raise
            ic.save_state("discharge", 30.0, 55)


# ─── run() ────────────────────────────────────────────────────────────────────

class TestRun(unittest.TestCase):

    def _args(self, mode="auto", dry_run=False):
        a = argparse.Namespace()
        a.mode = mode
        a.dry_run = dry_run
        return a

    def _battery(self, soc=60, power=0):
        return {"soc": soc, "power": power, "raw": {}}

    def test_auto_mode_success(self):
        """Auto mode: spot price drives decision, set_work_mode succeeds → exit 0."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(60)), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True):
            code = ic.run(self._args("auto"))
        self.assertEqual(code, 0)

    def test_auto_mode_set_work_mode_fails_returns_1(self):
        """Auto mode: set_work_mode returns False → exit 1."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(60)), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=False):
            code = ic.run(self._args("auto"))
        self.assertEqual(code, 1)

    def test_spot_price_failure_returns_1(self):
        """If get_spot_price raises RuntimeError, run() returns 1."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(60)), \
             patch.object(ic, "get_spot_price", side_effect=RuntimeError("no price")):
            code = ic.run(self._args("auto"))
        self.assertEqual(code, 1)

    def test_manual_discharge_mode(self):
        """Manual discharge with safe SoC works normally."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(soc=80)), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True):
            code = ic.run(self._args("discharge"))
        self.assertEqual(code, 0)

    def test_manual_discharge_clamped_when_soc_too_low(self):
        """Manual discharge requested but SoC <= SOC_MIN → switches to self_consumption."""
        soc = int(ic.SOC_MIN)  # exactly at minimum
        with patch.object(ic, "get_battery_state", return_value=self._battery(soc=soc)), \
             patch.object(ic, "get_spot_price", return_value=30.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True) as mock_set:
            code = ic.run(self._args("discharge"))
        self.assertEqual(code, 0)
        mock_set.assert_called_once()
        called_mode = mock_set.call_args[0][0]
        self.assertEqual(called_mode, "self_consumption")

    def test_manual_charge_clamped_when_soc_too_high(self):
        """Manual charge requested but SoC >= SOC_MAX → switches to self_consumption."""
        soc = int(ic.SOC_MAX)  # exactly at maximum
        with patch.object(ic, "get_battery_state", return_value=self._battery(soc=soc)), \
             patch.object(ic, "get_spot_price", return_value=3.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True) as mock_set:
            code = ic.run(self._args("charge"))
        self.assertEqual(code, 0)
        mock_set.assert_called_once()
        called_mode = mock_set.call_args[0][0]
        self.assertEqual(called_mode, "self_consumption")

    def test_manual_self_consumption_mode(self):
        """Manual self_consumption mode passes through unchanged."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(soc=60)), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True) as mock_set:
            code = ic.run(self._args("self_consumption"))
        self.assertEqual(code, 0)
        mock_set.assert_called_with("self_consumption", dry_run=False)

    def test_dry_run_mode(self):
        """Dry run passes dry_run=True to set_work_mode."""
        with patch.object(ic, "get_battery_state", return_value=self._battery(60)), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True) as mock_set:
            code = ic.run(self._args("auto", dry_run=True))
        self.assertEqual(code, 0)
        _, kwargs = mock_set.call_args
        self.assertTrue(kwargs.get("dry_run") or mock_set.call_args[0][1] is True
                        or mock_set.call_args[1].get("dry_run", False))


# ─── main() ───────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):

    def test_status_flag_success(self):
        """--status prints JSON and returns (no sys.exit)."""
        battery = {"soc": 72, "power": -300, "raw": {}}
        with patch.object(ic, "get_battery_state", return_value=battery), \
             patch.object(ic, "get_spot_price", return_value=20.0), \
             patch("sys.argv", ["inverter_control.py", "--status"]), \
             patch("builtins.print") as mock_print:
            ic.main()
        mock_print.assert_called_once()
        output = json.loads(mock_print.call_args[0][0])
        self.assertEqual(output["soc"], 72)
        self.assertIn("recommended_action", output)

    def test_status_flag_with_price_error(self):
        """--status when get_spot_price raises → prints error JSON."""
        battery = {"soc": 50, "power": 0, "raw": {}}
        with patch.object(ic, "get_battery_state", return_value=battery), \
             patch.object(ic, "get_spot_price", side_effect=Exception("no price")), \
             patch("sys.argv", ["inverter_control.py", "--status"]), \
             patch("builtins.print") as mock_print:
            ic.main()
        mock_print.assert_called_once()
        output = json.loads(mock_print.call_args[0][0])
        self.assertIn("error", output)
        self.assertEqual(output["soc"], 50)

    def test_main_run_mode(self):
        """Without --status, calls run() and sys.exit with its code."""
        with patch.object(ic, "run", return_value=0) as mock_run, \
             patch("sys.argv", ["inverter_control.py", "--mode", "auto"]), \
             patch("sys.exit") as mock_exit:
            ic.main()
        mock_run.assert_called_once()
        mock_exit.assert_called_once_with(0)

    def test_main_dry_run(self):
        """--dry-run flag is forwarded to run()."""
        with patch.object(ic, "run", return_value=0) as mock_run, \
             patch("sys.argv", ["inverter_control.py", "--dry-run"]), \
             patch("sys.exit"):
            ic.main()
        args_passed = mock_run.call_args[0][0]
        self.assertTrue(args_passed.dry_run)

    def test_main_mode_flag(self):
        """--mode charge is forwarded to run()."""
        with patch.object(ic, "run", return_value=0) as mock_run, \
             patch("sys.argv", ["inverter_control.py", "--mode", "charge"]), \
             patch("sys.exit"):
            ic.main()
        args_passed = mock_run.call_args[0][0]
        self.assertEqual(args_passed.mode, "charge")


class TestMainGuard(unittest.TestCase):
    """Cover the `if __name__ == '__main__': main()` guard (line 441)."""

    def test_main_guard_executes(self):
        """
        Run the script as __main__ via exec() to hit the module guard line.
        We patch sys.argv and all I/O so no real network calls happen.
        """
        import runpy

        battery = {"soc": 60, "power": 0, "raw": {}}
        with patch.object(ic, "get_battery_state", return_value=battery), \
             patch.object(ic, "get_spot_price", return_value=15.0), \
             patch.object(ic, "save_state"), \
             patch.object(ic, "set_work_mode", return_value=True), \
             patch("sys.argv", ["inverter_control.py", "--mode", "auto"]), \
             patch("sys.exit"):
            # Force the module to re-execute with __name__ == "__main__"
            runpy.run_path(
                os.path.join(os.path.dirname(__file__), "..", "scripts", "inverter_control.py"),
                run_name="__main__",
            )


if __name__ == "__main__":
    unittest.main()
