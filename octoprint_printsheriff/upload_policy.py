"""Which frames to upload to the inference server for later training."""

from __future__ import absolute_import


def should_upload(frame_index, probability, threshold, every_nth, uncertainty_margin):
    """Return whether a frame is worth collecting.

    A frame is uploaded when it is part of the regular sample (every Nth frame) or when the
    model was unsure about it, i.e. the probability sits within ``uncertainty_margin`` of the
    decision threshold. Uncertain frames are the most valuable ones for retraining.
    """
    every_nth = max(1, int(every_nth))
    if frame_index % every_nth == 0:
        return True

    if probability is None:
        return False

    return abs(float(probability) - float(threshold)) <= float(uncertainty_margin)
