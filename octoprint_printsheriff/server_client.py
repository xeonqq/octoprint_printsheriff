"""Stdlib HTTP client for the spaghetti inference server."""

from __future__ import absolute_import

import hashlib
import json
import os
import shutil
import socket
import urllib.error
import urllib.parse
import urllib.request


DOWNLOAD_CHUNK_BYTES = 64 * 1024


class InferenceServerClient(object):
    """Thin client over the inference server's HTTP API.

    Deliberately stdlib-only so the server-inference plugin keeps zero third-party
    dependencies.
    """

    def __init__(self, server_url, timeout_seconds=10):
        self.server_url = (server_url or "").rstrip("/")
        self.timeout_seconds = max(1, int(timeout_seconds))

    def _url(self, path, query=None):
        if not self.server_url:
            raise RuntimeError("Set the inference server URL in the PrintSheriff settings")
        url = self.server_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return url

    def _open(self, request):
        try:
            return urllib.request.urlopen(request, timeout=self.timeout_seconds)
        except (urllib.error.HTTPError, urllib.error.URLError) as error:
            if isinstance(error.reason, socket.gaierror):
                raise RuntimeError(
                    "Could not resolve host in inference server URL '{0}'. A docker-compose service "
                    "name like 'spaghetti-inference' only resolves inside that docker network - set "
                    "this to an address reachable from this device, e.g. http://<server-ip>:8080. "
                    "({1})".format(self.server_url, error)
                )
            raise RuntimeError("Inference server request failed: {0}".format(error))

    def predict(self, image_bytes):
        """POST an image to /v1/predict and return the failed probability."""
        request = urllib.request.Request(
            self._url("/v1/predict"),
            data=image_bytes,
            headers={"Content-Type": "image/jpeg"},
            method="POST",
        )
        with self._open(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        probability = payload.get("failed_probability")
        if not isinstance(probability, (int, float)) or not 0.0 <= probability <= 1.0:
            raise RuntimeError("Inference server returned an invalid failed_probability")
        return float(probability)

    def check_health(self):
        """GET /healthz and return its JSON payload, or raise if the server is unreachable."""
        request = urllib.request.Request(self._url("/healthz"), method="GET")
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def collect(self, image_bytes, failed_probability=None, source=None):
        """POST an image to the store-only /v1/collect endpoint for training data collection."""
        query = {}
        if failed_probability is not None:
            query["failed_probability"] = "{0:.6f}".format(float(failed_probability))
        if source:
            query["source"] = source

        request = urllib.request.Request(
            self._url("/v1/collect", query),
            data=image_bytes,
            headers={"Content-Type": "image/jpeg"},
            method="POST",
        )
        with self._open(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def fetch_model_metadata(self):
        """Return the served model's sha256, input geometry and preprocessing settings."""
        request = urllib.request.Request(self._url("/v1/model/metadata"), method="GET")
        with self._open(request) as response:
            payload = json.loads(response.read().decode("utf-8"))

        for key in ("sha256", "input_height", "input_width"):
            if not payload.get(key):
                raise RuntimeError("Inference server model metadata is missing '{0}'".format(key))
        return payload

    def download_model(self, destination_path):
        """Download the served model to ``destination_path`` and return its sha256.

        Streams into a sibling temporary file and renames only after a complete read so a
        failed download can never leave a truncated model behind.
        """
        request = urllib.request.Request(self._url("/v1/model"), method="GET")
        temporary_path = destination_path + ".tmp"
        digest = hashlib.sha256()

        directory = os.path.dirname(destination_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        try:
            with self._open(request) as response, open(temporary_path, "wb") as target:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    digest.update(chunk)
                    target.write(chunk)
            shutil.move(temporary_path, destination_path)
        except BaseException:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise

        return digest.hexdigest()
