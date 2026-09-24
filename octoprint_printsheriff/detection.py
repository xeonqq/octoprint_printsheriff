"""M-out-of-N sliding window failure detection state machine."""

from __future__ import absolute_import

from collections import deque


class DetectionState(object):
    """Track failed frames in a sliding window of size N requiring M failed frames to alert.

    The alert is one-shot per failure sequence: it triggers when the number of failed frames
    in the sliding window first reaches or exceeds M, and stays active until the count drops below M.
    """

    def __init__(self):
        self.history = deque()
        self.alert_active = False

    def reset(self):
        """Clear the sliding window history and alert flag."""
        self.history.clear()
        self.alert_active = False

    def update(self, probability, threshold, required_frames, window_size=None):
        """Feed one prediction in and return ``(failed_count, current_window_size, triggered)``.

        :param probability: Prediction failed probability (float 0.0-1.0).
        :param threshold: Probability threshold at/above which a frame counts as failed.
        :param required_frames: Minimum required failed frames in window to trigger alert (M).
        :param window_size: Sliding window size (N). Defaults to required_frames if not provided.
        """
        if window_size is None:
            window_size = required_frames

        n = max(1, int(window_size))
        m = max(1, min(int(required_frames), n))

        is_failed = float(probability) >= float(threshold)

        self.history.append(is_failed)
        while len(self.history) > n:
            self.history.popleft()

        failed_count = sum(1 for item in self.history if item)
        current_window_size = len(self.history)

        is_alert_condition = failed_count >= m

        if is_alert_condition:
            if not self.alert_active:
                triggered = True
                self.alert_active = True
            else:
                triggered = False
        else:
            self.alert_active = False
            triggered = False

        return failed_count, current_window_size, triggered
