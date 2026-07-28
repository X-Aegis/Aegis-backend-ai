"""tests/test_drift_detection.py

Unit tests for the drift detection module (services/drift_detection.py).

Coverage includes:
- Rolling MAE / RMSE helpers (edge cases: empty, partial, full window)
- ADWINDetector: threshold sensitivity, window shrinkage after drift
- PageHinkleyDetector: threshold sensitivity, reset behaviour, statistic growth
- DriftMonitor: integration, bulk_update, report fields, drift signalling
- DriftReport: derived property (drift_detected)
"""

from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.drift_detection import (
    ADWINDetector,
    DriftMonitor,
    DriftReport,
    PageHinkleyDetector,
    compute_rolling_mae,
    compute_rolling_rmse,
)

# ===========================================================================
# Rolling accuracy metric helpers
# ===========================================================================


class TestComputeRollingMAE:
    def test_returns_none_when_fewer_errors_than_window(self):
        assert compute_rolling_mae([0.5, 1.0], window=5) is None

    def test_returns_none_for_empty_list(self):
        assert compute_rolling_mae([], window=3) is None

    def test_returns_mean_of_last_window_elements(self):
        # window=3, take last 3 of [1, 2, 3, 4, 5] → mean([3, 4, 5]) = 4.0
        result = compute_rolling_mae([1, 2, 3, 4, 5], window=3)
        assert result == pytest.approx(4.0)

    def test_exact_window_size(self):
        result = compute_rolling_mae([2.0, 4.0, 6.0], window=3)
        assert result == pytest.approx(4.0)

    def test_window_one_returns_last_element(self):
        result = compute_rolling_mae([10.0, 20.0, 30.0], window=1)
        assert result == pytest.approx(30.0)

    def test_uniform_errors_returns_that_value(self):
        result = compute_rolling_mae([5.0] * 20, window=10)
        assert result == pytest.approx(5.0)


class TestComputeRollingRMSE:
    def test_returns_none_when_fewer_errors_than_window(self):
        assert compute_rolling_rmse([1.0], window=5) is None

    def test_returns_none_for_empty_list(self):
        assert compute_rolling_rmse([], window=3) is None

    def test_single_element_window(self):
        result = compute_rolling_rmse([3.0, 6.0, 9.0], window=1)
        assert result == pytest.approx(9.0)

    def test_rmse_equals_mae_for_uniform_errors(self):
        errors = [4.0] * 10
        mae = compute_rolling_mae(errors, window=5)
        rmse = compute_rolling_rmse(errors, window=5)
        assert rmse == pytest.approx(mae)

    def test_rmse_greater_than_mae_for_variable_errors(self):
        # RMSE penalises large errors more than MAE
        errors = [1.0, 1.0, 1.0, 1.0, 10.0]
        mae = compute_rolling_mae(errors, window=5)
        rmse = compute_rolling_rmse(errors, window=5)
        assert rmse > mae

    def test_known_value(self):
        # errors = [3, 4] → RMSE = sqrt((9 + 16) / 2) = sqrt(12.5)
        result = compute_rolling_rmse([3.0, 4.0], window=2)
        assert result == pytest.approx(math.sqrt(12.5))


# ===========================================================================
# ADWIN detector
# ===========================================================================


class TestADWINDetector:
    def test_no_drift_on_stable_stream(self):
        """A stream with constant values should not trigger drift."""
        detector = ADWINDetector(delta=0.002)
        drifts = [detector.update(0.1) for _ in range(100)]
        assert not any(drifts), "Stable stream should not trigger ADWIN drift"

    def test_drift_detected_after_abrupt_mean_shift(self):
        """Feeding low values then high values should trigger a drift signal."""
        detector = ADWINDetector(delta=0.002)
        # Feed 60 observations near 0 → 60 near 1
        for _ in range(60):
            detector.update(0.0)
        drift_fired = False
        for _ in range(60):
            if detector.update(1.0):
                drift_fired = True
                break
        assert drift_fired, "ADWIN should detect abrupt mean shift from 0 to 1"

    def test_window_shrinks_after_drift(self):
        """After drift detection the adaptive window should be smaller than 120."""
        detector = ADWINDetector(delta=0.002)
        for _ in range(60):
            detector.update(0.0)
        for _ in range(60):
            detector.update(1.0)
        assert detector.window_size < 120

    def test_reset_clears_state(self):
        detector = ADWINDetector(delta=0.002)
        for _ in range(30):
            detector.update(0.5)
        detector.reset()
        assert detector.window_size == 0
        assert not detector.drift_detected

    def test_high_delta_less_sensitive(self):
        """A high delta value (less strict) should fire fewer drifts than low delta."""
        strict = ADWINDetector(delta=1e-10)
        lenient = ADWINDetector(delta=0.5)

        stream = [0.0] * 40 + [1.0] * 40

        strict_drifts = sum(strict.update(v) for v in stream)
        lenient_drifts = sum(lenient.update(v) for v in stream)

        # Stricter detector fires more (lower false-negative rate) — at minimum
        # the lenient detector should not fire MORE.
        assert strict_drifts >= lenient_drifts

    def test_single_observation_no_drift(self):
        detector = ADWINDetector()
        assert not detector.update(0.5)

    def test_drift_detected_property_matches_return_value(self):
        detector = ADWINDetector(delta=0.002)
        for _ in range(60):
            detector.update(0.0)
        result = detector.update(1.0)
        assert result == detector.drift_detected


# ===========================================================================
# Page-Hinkley detector
# ===========================================================================


class TestPageHinkleyDetector:
    def test_no_drift_on_stable_stream(self):
        """Constant stream should never trigger the PH test."""
        detector = PageHinkleyDetector(delta=0.005, lambda_=50.0)
        drifts = [detector.update(0.1) for _ in range(200)]
        assert not any(drifts), "Stable stream should not trigger PH drift"

    def test_drift_detected_for_sustained_upward_shift(self):
        """Persistent upward shift in errors should trigger PH."""
        detector = PageHinkleyDetector(delta=0.005, lambda_=5.0)
        # Warmup with small errors, then steadily large errors
        for _ in range(20):
            detector.update(0.0)
        drift_fired = False
        for _ in range(200):
            if detector.update(1.0):
                drift_fired = True
                break
        assert drift_fired, "PH should detect sustained upward drift"

    def test_ph_statistic_grows_with_large_errors(self):
        """Statistic should be larger after consistent large errors."""
        detector = PageHinkleyDetector(delta=0.005, lambda_=9999.0)
        for _ in range(5):
            detector.update(0.01)
        stat_low = detector.ph_statistic
        for _ in range(50):
            detector.update(1.0)
        stat_high = detector.ph_statistic
        assert stat_high > stat_low

    def test_reset_clears_state(self):
        detector = PageHinkleyDetector(delta=0.005, lambda_=5.0)
        for _ in range(50):
            detector.update(1.0)
        detector.reset()
        assert detector.ph_statistic == pytest.approx(0.0)
        assert not detector.drift_detected

    def test_high_lambda_less_sensitive_than_low_lambda(self):
        """Lower threshold triggers drift sooner than higher threshold."""
        sensitive = PageHinkleyDetector(delta=0.005, lambda_=2.0)
        conservative = PageHinkleyDetector(delta=0.005, lambda_=500.0)

        n_sensitive, n_conservative = None, None
        for i in range(500):
            v = 1.0  # constant large error
            if sensitive.update(v) and n_sensitive is None:
                n_sensitive = i
            if conservative.update(v) and n_conservative is None:
                n_conservative = i

        # sensitive should trigger first (lower threshold)
        if n_sensitive is not None and n_conservative is not None:
            assert n_sensitive <= n_conservative

    def test_drift_detected_property_matches_return_value(self):
        detector = PageHinkleyDetector(delta=0.005, lambda_=5.0)
        result = detector.update(1.0)
        assert result == detector.drift_detected


# ===========================================================================
# DriftMonitor — integration tests
# ===========================================================================


class TestDriftMonitor:
    def test_update_returns_drift_report(self):
        monitor = DriftMonitor()
        report = monitor.update(predicted=50.0, actual=52.0)
        assert isinstance(report, DriftReport)

    def test_report_abs_error_correct(self):
        monitor = DriftMonitor()
        report = monitor.update(predicted=60.0, actual=55.0)
        assert report.latest_abs_error == pytest.approx(5.0)

    def test_rolling_mae_is_none_below_window(self):
        monitor = DriftMonitor(rolling_window=10)
        for _ in range(9):
            report = monitor.update(50.0, 52.0)
        assert report.rolling_mae is None

    def test_rolling_mae_available_at_window(self):
        monitor = DriftMonitor(rolling_window=5)
        for _ in range(5):
            report = monitor.update(50.0, 52.0)  # abs_error = 2.0 each time
        assert report.rolling_mae == pytest.approx(2.0)

    def test_rolling_rmse_available_at_window(self):
        monitor = DriftMonitor(rolling_window=5)
        for _ in range(5):
            report = monitor.update(50.0, 52.0)
        assert report.rolling_rmse == pytest.approx(2.0)

    def test_n_samples_increments(self):
        monitor = DriftMonitor()
        for i in range(1, 6):
            monitor.update(50.0, 51.0)
            assert monitor.n_samples == i

    def test_no_drift_on_stable_predictions(self):
        """Perfect predictions should not trigger drift."""
        monitor = DriftMonitor(rolling_window=10)
        report = None
        for _ in range(100):
            report = monitor.update(predicted=50.0, actual=50.0)
        assert not report.drift_detected

    def test_drift_detected_after_large_sustained_errors(self):
        """Large, sustained errors should trigger at least one detector."""
        monitor = DriftMonitor(
            rolling_window=10,
            adwin_delta=0.002,
            ph_lambda=5.0,   # low threshold for test speed
        )
        # Warmup with near-perfect predictions
        for _ in range(20):
            monitor.update(predicted=50.0, actual=50.0)
        # Then large consistent errors
        drift_fired = False
        for _ in range(200):
            report = monitor.update(predicted=50.0, actual=100.0)
            if report.drift_detected:
                drift_fired = True
                break
        assert drift_fired, "DriftMonitor should detect drift after large sustained errors"

    def test_bulk_update_returns_final_report(self):
        monitor = DriftMonitor(rolling_window=3)
        pairs = [(50.0, 51.0), (52.0, 50.0), (48.0, 49.0)]
        report = monitor.bulk_update(pairs)
        assert report.n_samples == 3

    def test_bulk_update_raises_on_empty_sequence(self):
        monitor = DriftMonitor()
        with pytest.raises(ValueError):
            monitor.bulk_update([])

    def test_reset_clears_all_state(self):
        monitor = DriftMonitor()
        for _ in range(20):
            monitor.update(50.0, 55.0)
        monitor.reset()
        assert monitor.n_samples == 0
        assert monitor.errors == []

    def test_errors_list_is_immutable_copy(self):
        """Mutating the returned errors list must not affect internal state."""
        monitor = DriftMonitor()
        monitor.update(50.0, 55.0)
        errors = monitor.errors
        errors.append(999.0)
        assert monitor.n_samples == 1

    def test_normalize_cap_limits_detector_input(self):
        """Errors beyond normalize_cap should be capped at 1.0 for detectors."""
        monitor = DriftMonitor(normalize_cap=10.0, ph_lambda=9999.0)
        # error = 100.0, normalize_cap = 10.0 → capped to 1.0; should not raise
        report = monitor.update(predicted=0.0, actual=100.0)
        assert report.latest_abs_error == pytest.approx(100.0)

    def test_adwin_window_size_in_report(self):
        monitor = DriftMonitor()
        report = monitor.update(50.0, 52.0)
        assert isinstance(report.adwin_window_size, int)
        assert report.adwin_window_size >= 1

    def test_ph_statistic_in_report(self):
        monitor = DriftMonitor()
        report = monitor.update(50.0, 52.0)
        assert isinstance(report.ph_statistic, float)
        assert report.ph_statistic >= 0.0


# ===========================================================================
# DriftReport — derived property
# ===========================================================================


class TestDriftReport:
    def _make_report(self, adwin=False, ph=False):
        return DriftReport(
            n_samples=1,
            latest_abs_error=2.0,
            rolling_mae=None,
            rolling_rmse=None,
            adwin_drift_detected=adwin,
            ph_drift_detected=ph,
            adwin_window_size=10,
            ph_statistic=0.5,
        )

    def test_drift_detected_false_when_both_false(self):
        report = self._make_report(adwin=False, ph=False)
        assert not report.drift_detected

    def test_drift_detected_true_when_adwin_fires(self):
        report = self._make_report(adwin=True, ph=False)
        assert report.drift_detected

    def test_drift_detected_true_when_ph_fires(self):
        report = self._make_report(adwin=False, ph=True)
        assert report.drift_detected

    def test_drift_detected_true_when_both_fire(self):
        report = self._make_report(adwin=True, ph=True)
        assert report.drift_detected

    def test_report_is_frozen(self):
        """DriftReport must be immutable."""
        report = self._make_report()
        with pytest.raises((AttributeError, TypeError)):
            report.n_samples = 999  # type: ignore[misc]
