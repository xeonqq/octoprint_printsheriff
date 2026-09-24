"""Shared image preprocessing for spaghetti-failure training and inference.

This is the single implementation used by training, the inference server and the local
OctoPrint plugin, so a model always sees identically prepared pixels wherever it runs.

Pillow-based on purpose: the plugin runs on the printer host and cannot depend on OpenCV.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image


def center_square_crop_rgb(image: np.ndarray, crop_fraction: float = 1.0) -> np.ndarray:
    """Return a centered square RGB crop without changing image geometry."""
    if not 0.1 <= crop_fraction <= 1.0:
        raise ValueError("crop_fraction must be between 0.1 and 1.0")

    height, width = image.shape[:2]
    crop_size = max(1, int(min(height, width) * crop_fraction))
    left = (width - crop_size) // 2
    top = (height - crop_size) // 2
    return image[top : top + crop_size, left : left + crop_size]


def resize_rgb(image: np.ndarray, target_height: int, target_width: int) -> np.ndarray:
    """Resize an RGB image with Pillow's antialiased bilinear filter."""
    # Image.fromarray needs a contiguous buffer; crops are views into the source array.
    source = Image.fromarray(np.ascontiguousarray(image, dtype=np.uint8))
    resized = source.resize((target_width, target_height), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=np.float32)


def preprocess_rgb_image(
    image: np.ndarray,
    target_height: int,
    target_width: int,
    crop_fraction: float = 1.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Center-crop an RGB image to square and resize it for a model input."""
    center_crop = center_square_crop_rgb(image, crop_fraction)
    return center_crop, resize_rgb(center_crop, target_height, target_width)


def decode_rgb_bytes(image_bytes: bytes) -> np.ndarray:
    """Decode encoded image bytes into a uint8 RGB array."""
    with Image.open(io.BytesIO(image_bytes)) as opened:
        return np.asarray(opened.convert("RGB"), dtype=np.uint8)


def preprocess_rgb_bytes(
    image_bytes: bytes,
    target_height: int,
    target_width: int,
    crop_fraction: float = 1.0,
) -> np.ndarray:
    """Decode, center-crop and resize encoded image bytes into model input pixels."""
    _, resized = preprocess_rgb_image(
        decode_rgb_bytes(image_bytes), target_height, target_width, crop_fraction
    )
    return resized
