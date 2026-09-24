"""Download and cache the TFLite model served by the inference server."""

from __future__ import absolute_import

import json
import os


MODEL_FILENAME = "model_float32.tflite"
METADATA_FILENAME = "model_meta.json"


class ModelSync(object):
    """Keep a local copy of the server's model, re-downloading only when its sha256 changes."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        self.model_path = os.path.join(cache_dir, MODEL_FILENAME)
        self.metadata_path = os.path.join(cache_dir, METADATA_FILENAME)

    def load_cached(self):
        """Return the cached metadata when a usable model is already on disk, else ``None``."""
        if not os.path.isfile(self.model_path) or not os.path.isfile(self.metadata_path):
            return None
        try:
            with open(self.metadata_path, "r", encoding="utf-8") as metadata_file:
                metadata = json.load(metadata_file)
        except (OSError, ValueError):
            return None
        if not metadata.get("sha256"):
            return None
        return metadata

    def sync(self, client):
        """Fetch the server's model if it differs from the cache.

        Returns ``(metadata, changed)``. The cached model is left untouched when the download
        or checksum verification fails.
        """
        metadata = client.fetch_model_metadata()
        cached = self.load_cached()
        if cached is not None and cached.get("sha256") == metadata["sha256"]:
            return cached, False

        os.makedirs(self.cache_dir, exist_ok=True)
        downloaded_sha256 = client.download_model(self.model_path)
        if downloaded_sha256 != metadata["sha256"]:
            os.remove(self.model_path)
            raise RuntimeError(
                "Downloaded model checksum {0} does not match the advertised {1}".format(
                    downloaded_sha256, metadata["sha256"]
                )
            )

        with open(self.metadata_path, "w", encoding="utf-8") as metadata_file:
            json.dump(metadata, metadata_file)
        return metadata, True
