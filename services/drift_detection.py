"""services/drift_detection.py

Model drift detection for the X-Aegis FX volatility forecasting engine.

Implements two complementary, statistically principled drift detectors:

* **ADWIN (ADaptive WINdowing)** — A change-point detector that maintains a
  sliding window whose size adapts automatically when the mean of the stream
  deviates beyond a confidence bound.  Sensitive to both gradual and abrupt
  drift.

* **Page-Hinkley Test** — A sequential-analysis test based on cumulative sums
  that fires when the running mean drifts by more than a configurable
  threshold.  Lightweight and suited to detecting gradual upward drift in
  prediction error.

Both detectors operate on a *stream of per-prediction absolute errors*
(|predicted − actual|), so a drift signal means "the model is getting
systematically worse."

Rolling accuracy metrics (MAE, RMSE) are computed over a configurable window
of the same error stream, and are returned alongside the drift signals so
callers can attach them to every monitoring report.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Rolling accuracy metrics
# ---------------------------------------------------------------------------

def compute_rolling_mae(errors: Sequence[float], window: int) -> float | None:
    """Return MAE over the most recent *window* absolute errors, or None if
    fewer than *window* observations are available."""
    if len(errors) < window:
        return None
    recent = list(errors)[-window:]
    return sum(recent) / len(recent)


def compute_rolling_rmse(errors: Sequence[float], window: int) -> float | None:
    """Return RMSE over the most recent *window* absolute errors, or None if
    fewer than *window* observations are available."""
    if len(errors) < window:
        return None
    recent = list(errors)[-window:]
    return math.sqrt(sum(e ** 2 for e in recent) / len(recent))


# ---------------------------------------------------------------------------
# ADWIN detector
# ---------------------------------------------------------------------------

@dataclass
class ADWINDetector:
    """Adaptive Windowing (ADWIN) change detector.

    Parameters
    ----------
    delta:
        Confidence parameter (0 < delta < 1).  Smaller values make the
        detector less sensitive (fewer false alarms).  Typical default: 0.002.

    How it works
    ------------
    ADWIN maintains a window ``W`` of the most recently observed values.  On
    every new observation it tests all possible splits of ``W`` into a left
    sub-window ``W0`` and right sub-window ``W1``.  If the means of ``W0`` and
    ``W1`` differ by more than the bound derived from the Hoeffding inequality
    (scaled by ``delta``), ADWIN concludes a drift has occurred and shrinks the
    window to ``W1`` (the more recent half).
    """

    delta: float = 0.002
    _window: deque = field(default_factory=deque, init=False, repr=False)
    _drift_detected: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def update(self, value: float) -> bool:
        """Ingest one new observation.  Returns ``True`` if drift is detected."""
        self._window.append(value)
        self._drift_detected = self._check_drift()
        return self._drift_detected

    @property
    def drift_detected(self) -> bool:
        """``True`` if the last :meth:`update` call detected drift."""
        return self._drift_detected

    @property
    def window_size(self) -> int:
        """Number of samples currently held in the adaptive window."""
        return len(self._window)

    def reset(self) -> None:
        """Clear all state (use after a confirmed concept drift)."""
        self._window.clear()
        self._drift_detected = False

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _check_drift(self) -> bool:
        """Scan all splits of the current window for a statistically
        significant mean shift.  O(n²) in the worst case for large windows,
        but in practice ADWIN keeps the window small after each drift event."""
        n = len(self._window)
        if n < 2:
            return False

        w = list(self._window)
        total = sum(w)

        # Incrementally build right-window sums by scanning from the right.
        right_sum = 0.0
        for cut in range(n - 1, 0, -1):
            right_sum += w[cut]
            left_sum = total - right_sum
            n0 = n - cut   # right sub-window size
            n1 = cut       # left sub-window size

            mean0 = right_sum / n0
            mean1 = left_sum / n1

            # Hoeffding bound — assumes values in [0, 1]; for normalised
            # absolute errors this is a reasonable assumption.
            m = 1.0 / n0 + 1.0 / n1
            epsilon_cut = math.sqrt(m / 2.0 * math.log(4.0 * n / self.delta))

            if abs(mean0 - mean1) >= epsilon_cut:
                # Drift detected — shrink window to the right sub-window.
                for _ in range(n1):
                    self._window.popleft()
                return True

        return False


# ---------------------------------------------------------------------------
# Page-Hinkley detector
# ---------------------------------------------------------------------------

@dataclass
class PageHinkleyDetector:
    """Page-Hinkley sequential drift detector.

    Parameters
    ----------
    delta:
        Minimal magnitude of change to detect.  Acts as an allowance for
        natural fluctuation.  Default: 0.005.
    lambda_:
        Detection threshold.  When the Page-Hinkley statistic exceeds this,
        drift is signalled.  Default: 50.
    alpha:
        Forgetting factor applied to the running mean (0 < alpha ≤ 1).
        Values close to 1 weight all past observations equally; smaller values
        give more weight to recent data.  Default: 1.0 (no forgetting).

    Notes
    -----
    The test monitors *upward* drift in error magnitude (i.e. the model
    getting worse over time).  If you want to detect both directions, run two
    instances — one on the raw errors and one on the negated errors.
    """

    delta: float = 0.005
    lambda_: float = 50.0
    alpha: float = 1.0

    _sum: float = field(default=0.0, init=False)
    _min_sum: float = field(default=0.0, init=False)
    _n: int = field(default=0, init=False)
    _mean: float = field(default=0.0, init=False)
    _drift_detected: bool = field(default=False, init=False)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def update(self, value: float) -> bool:
        """Ingest one new observation.  Returns ``True`` if drift is detected."""
        self._n += 1
        # Running mean with optional forgetting
        self._mean = self.alpha * self._mean + (1 - self.alpha) * value if self._n > 1 else value

        self._sum += value - self._mean - self.delta
        self._min_sum = min(self._min_sum, self._sum)

        ph_statistic = self._sum - self._min_sum
        self._drift_detected = ph_statistic > self.lambda_
        return self._drift_detected

    @property
    def drift_detected(self) -> bool:
        """``True`` if the last :meth:`update` call detected drift."""
        return self._drift_detected

    @property
    def ph_statistic(self) -> float:
        """Current value of the Page-Hinkley test statistic."""
        return self._sum - self._min_sum

    def reset(self) -> None:
        """Reset all accumulated state."""
        self._sum = 0.0
        self._min_sum = 0.0
        self._n = 0
        self._mean = 0.0
        self._drift_detected = False


# ---------------------------------------------------------------------------
# High-level DriftMonitor
# ---------------------------------------------------------------------------

@dataclass
class DriftMonitor:
    """Stateful monitor that accumulates prediction errors and runs both
    ADWIN and Page-Hinkley detectors on each new observation.

    Parameters
    ----------
    rolling_window:
        Number of recent errors used for rolling MAE and RMSE.  Default: 50.
    adwin_delta:
        ADWIN confidence parameter.  Default: 0.002.
    ph_delta:
        Page-Hinkley allowance (delta).  Default: 0.005.
    ph_lambda:
        Page-Hinkley detection threshold.  Default: 50.
    ph_alpha:
        Page-Hinkley forgetting factor.  Default: 1.0.
    normalize_cap:
        If provided, absolute errors are capped and divided by this value
        before being fed to the detectors (keeps inputs in [0, 1]).
        Default: 100.0.
    """

    rolling_window: int = 50
    adwin_delta: float = 0.002
    ph_delta: float = 0.005
    ph_lambda: float = 50.0
    ph_alpha: float = 1.0
    normalize_cap: float = 100.0

    _errors: list[float] = field(default_factory=list, init=False, repr=False)
    _adwin: ADWINDetector = field(init=False, repr=False)
    _ph: PageHinkleyDetector = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._adwin = ADWINDetector(delta=self.adwin_delta)
        self._ph = PageHinkleyDetector(
            delta=self.ph_delta,
            lambda_=self.ph_lambda,
            alpha=self.ph_alpha,
        )

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def update(self, predicted: float, actual: float) -> DriftReport:
        """Register a new (predicted, actual) pair and return a :class:`DriftReport`."""
        abs_error = abs(predicted - actual)
        self._errors.append(abs_error)

        # Normalise to [0, 1] for the detectors.
        normalised = min(abs_error / self.normalize_cap, 1.0)
        adwin_drift = self._adwin.update(normalised)
        ph_drift = self._ph.update(normalised)

        return DriftReport(
            n_samples=len(self._errors),
            latest_abs_error=abs_error,
            rolling_mae=compute_rolling_mae(self._errors, self.rolling_window),
            rolling_rmse=compute_rolling_rmse(self._errors, self.rolling_window),
            adwin_drift_detected=adwin_drift,
            ph_drift_detected=ph_drift,
            adwin_window_size=self._adwin.window_size,
            ph_statistic=self._ph.ph_statistic,
        )

    def bulk_update(self, pairs: Sequence[tuple[float, float]]) -> DriftReport:
        """Process a sequence of ``(predicted, actual)`` pairs in order and
        return the report for the final observation."""
        report = None
        for predicted, actual in pairs:
            report = self.update(predicted, actual)
        if report is None:
            raise ValueError("bulk_update requires at least one (predicted, actual) pair.")
        return report

    @property
    def n_samples(self) -> int:
        """Total number of observations ingested so far."""
        return len(self._errors)

    @property
    def errors(self) -> list[float]:
        """All absolute errors observed so far (immutable snapshot)."""
        return list(self._errors)

    def reset(self) -> None:
        """Reset all accumulated state in all sub-detectors."""
        self._errors.clear()
        self._adwin.reset()
        self._ph.reset()


# ---------------------------------------------------------------------------
# DriftReport — immutable result object
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DriftReport:
    """Snapshot of the monitor's state after processing one observation.

    Attributes
    ----------
    n_samples:
        Total observations processed so far.
    latest_abs_error:
        |predicted − actual| for the most recent observation.
    rolling_mae:
        Mean absolute error over the last ``rolling_window`` samples, or
        ``None`` if fewer samples have been seen.
    rolling_rmse:
        Root mean squared error over the last ``rolling_window`` samples, or
        ``None`` if fewer samples have been seen.
    adwin_drift_detected:
        ``True`` if ADWIN signalled a drift on this observation.
    ph_drift_detected:
        ``True`` if the Page-Hinkley test signalled a drift on this
        observation.
    drift_detected:
        ``True`` if *either* detector fired.
    adwin_window_size:
        Current number of samples in the ADWIN adaptive window.
    ph_statistic:
        Current value of the Page-Hinkley test statistic.
    """

    n_samples: int
    latest_abs_error: float
    rolling_mae: float | None
    rolling_rmse: float | None
    adwin_drift_detected: bool
    ph_drift_detected: bool
    adwin_window_size: int
    ph_statistic: float

    @property
    def drift_detected(self) -> bool:
        """``True`` if *either* ADWIN or Page-Hinkley fired."""
        return self.adwin_drift_detected or self.ph_drift_detected
