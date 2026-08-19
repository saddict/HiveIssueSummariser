"""Tests for the thresholds file and its loader.

The point of thresholds.toml is that it is the *only* place a tunable value is
written down. These tests defend that property: the file must cover everything
the code asks of it, a missing or mistyped key must fail loudly rather than fall
back to an invisible default, and the override hatches must actually override.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from beemon_scoring import events, metrics, quality, scoring, sister_comparison, weather
from beemon_scoring.thresholds import (
    DEFAULT_PATH,
    THRESHOLDS_PATH,
    _coerce,
    _env_name,
    number,
    section,
    text,
    value,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ThresholdsFileTests(unittest.TestCase):
    def test_the_default_file_is_the_one_in_the_repo_root(self) -> None:
        self.assertEqual(DEFAULT_PATH, PROJECT_ROOT / "thresholds.toml")
        self.assertTrue(DEFAULT_PATH.exists())

    def test_missing_key_raises_instead_of_defaulting(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            value("status.no_such_threshold")
        self.assertIn("status.no_such_threshold", str(caught.exception))
        self.assertIn(str(THRESHOLDS_PATH), str(caught.exception))

    def test_section_returns_every_metric_weight(self) -> None:
        weights = section("metric_weights")
        self.assertEqual(set(weights), {metric.name for metric in metrics.METRICS})
        # Renormalized to sum to 1.0 when the catalog dropped to 5 metrics (§24).
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)

    def test_asking_a_section_for_a_scalar_is_an_error(self) -> None:
        with self.assertRaises(RuntimeError):
            section("status.watch_score")

    def test_every_module_constant_matches_the_file(self) -> None:
        """Each module reads its own constants; if one is wired to the wrong key
        the values silently diverge from the file a reviewer is auditing."""
        self.assertEqual(events.ADDITION_FLOOR_KG, number("events.addition_floor_kg"))
        self.assertEqual(events.SWARM_TEMP_SWING_F, number("events.swarm.temp_swing_f"))
        self.assertEqual(quality.MAX_WEIGHT_JUMP_KG, number("quality.jumps.max_weight_jump_kg"))
        self.assertEqual(quality.MIN_WEIGHT_KG, number("quality.bounds.min_weight_kg"))
        self.assertEqual(metrics.BADNESS_Z_SCORE_SCALE, number("scoring.badness_z_score_scale"))
        self.assertEqual(scoring.UNDERPERFORMING_SCORE, number("status.underperforming_score"))
        self.assertEqual(scoring.WATCH_SCORE, number("status.watch_score"))
        self.assertEqual(weather.POOR_WEATHER_LOW_TEMP_F, number("weather.poor_low_temp_f"))
        self.assertEqual(sister_comparison.SIMILAR_GAP, number("sister.similar_gap"))

    def test_timezone_is_configured_not_hardcoded_to_the_system_zone(self) -> None:
        self.assertEqual(text("data.default_timezone"), "America/New_York")


class OverrideTests(unittest.TestCase):
    def test_env_name_derives_from_the_dotted_path(self) -> None:
        self.assertEqual(_env_name("status.watch_score"), "BEEMON_STATUS_WATCH_SCORE")
        self.assertEqual(_env_name("quality.jumps.max_temp_jump_f"), "BEEMON_QUALITY_JUMPS_MAX_TEMP_JUMP_F")

    def test_overrides_keep_the_type_used_in_the_file(self) -> None:
        self.assertEqual(_coerce("25", "status.watch_score", 30.0), 25.0)
        self.assertIsInstance(_coerce("25", "status.watch_score", 30.0), float)
        self.assertEqual(_coerce("4", "status.underperforming_flag_count", 3), 4)
        self.assertIsInstance(_coerce("4", "status.underperforming_flag_count", 3), int)
        self.assertEqual(_coerce("UTC", "data.default_timezone", "America/New_York"), "UTC")

    def test_a_non_numeric_override_of_a_number_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            _coerce("soon", "status.max_reporting_gap_days", 1.0)

    def test_env_var_overrides_a_single_value(self) -> None:
        # Values are read at import, so this runs in a fresh interpreter.
        result = self._run_with_env(
            {"BEEMON_STATUS_WATCH_SCORE": "12.5"},
            "from beemon_scoring.scoring import WATCH_SCORE; print(WATCH_SCORE)",
        )
        self.assertEqual(result, "12.5")

    def test_env_var_points_at_an_alternate_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alternate = Path(tmp) / "alt.toml"
            alternate.write_text(
                DEFAULT_PATH.read_text().replace("watch_score = 30.0", "watch_score = 99.0")
            )
            result = self._run_with_env(
                {"BEEMON_THRESHOLDS": str(alternate)},
                "from beemon_scoring.scoring import WATCH_SCORE; print(WATCH_SCORE)",
            )
        self.assertEqual(result, "99.0")

    def test_a_missing_thresholds_file_fails_with_a_clear_message(self) -> None:
        result = self._run_with_env(
            {"BEEMON_THRESHOLDS": "/nonexistent/thresholds.toml"},
            "try:\n import beemon_scoring.scoring\nexcept RuntimeError as error:\n print('RAISED' if 'not found' in str(error) else 'WRONG')",
        )
        self.assertEqual(result, "RAISED")

    def _run_with_env(self, env: dict[str, str], code: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=PROJECT_ROOT,
            env={**os.environ, **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return completed.stdout.strip()


class HiveConfigTests(unittest.TestCase):
    def test_a_setting_left_in_hive_config_is_rejected_not_ignored(self) -> None:
        from beemon_scoring.data_loader import load_hive_config

        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "hive_config.py"
            stale.write_text(
                (PROJECT_ROOT / "hive_config.py").read_text() + "\nMIN_REGION_SITE_COUNT = 5\n"
            )
            with self.assertRaises(RuntimeError) as caught:
                load_hive_config(stale)

        message = str(caught.exception)
        self.assertIn("MIN_REGION_SITE_COUNT", message)
        self.assertIn("regions.min_region_site_count", message)

    def test_the_shipped_hive_config_holds_no_tunables(self) -> None:
        from beemon_scoring.data_loader import _MOVED_SETTINGS, load_hive_config

        _hives, _sides, settings = load_hive_config(PROJECT_ROOT / "hive_config.py")

        self.assertEqual(settings["min_region_site_count"], 2)
        self.assertEqual(settings["rolling_window_days"], number("window.rolling_window_days"))
        source = (PROJECT_ROOT / "hive_config.py").read_text()
        for name in _MOVED_SETTINGS:
            self.assertNotIn(f"\n{name} =", source)


if __name__ == "__main__":
    unittest.main()
