"""OctoPrint plugin for detecting spaghetti failures in timelapse captures.

Classification runs either on the inference server or locally on the printer host, selected by
the ``inference_mode`` setting. Local mode additionally uploads sampled frames back to the server
so they can be labelled and used for retraining.
"""

from __future__ import absolute_import

import math
import os
import queue
import re
import threading
import time

import flask
import octoprint.plugin
from octoprint.events import Events

from .detection import DetectionState
from .model_sync import ModelSync
from .ntfy_notifier import send_ntfy_notification
from .server_client import InferenceServerClient
from .upload_policy import should_upload

try:
    from .preprocessing import preprocess_rgb_bytes
except ImportError as exc:
    # pip cannot install system packages, so surface a clear fix instead of numpy's raw traceback.
    raise ImportError(
        "OctoPrint-PrintSheriff could not import numpy. On Raspberry Pi/OctoPi this is usually "
        "a missing system OpenBLAS library. Install it and restart OctoPrint:\n"
        "  sudo apt-get update && sudo apt-get install -y libopenblas0-pthread\n"
        f"Original error: {exc}"
    ) from exc


MODE_SERVER = "server"
MODE_LOCAL = "local"
DEFAULT_SERVER_URL = "https://api.printsheriff.com"

# A single local inference slower than this makes the printer host a poor fit for local mode.
SLOW_INFERENCE_MS = 1500

# Real-time capture never runs faster than this, regardless of the configured setting.
MIN_REALTIME_INTERVAL_SECONDS = 1.0

# How often the last-extrusion-move snapshot (see on_gcode_queuing) is allowed to refresh.
LAST_EXTRUSION_CAPTURE_MIN_INTERVAL_SECONDS = 3.0

# A G0/G1/G2/G3 line that is actually depositing filament, e.g. "G1 X10 Y20 E1.234 F1500".
_EXTRUSION_MOVE_RE = re.compile(r"\bE-?[0-9]*\.?[0-9]+")


class PrintSheriffPlugin(
    octoprint.plugin.AssetPlugin,
    octoprint.plugin.EventHandlerPlugin,
    octoprint.plugin.SettingsPlugin,
    octoprint.plugin.ShutdownPlugin,
    octoprint.plugin.SimpleApiPlugin,
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.TemplatePlugin,
):
    """Evaluate each timelapse capture in a serial worker thread."""

    def __init__(self):
        self._detection = DetectionState()
        self._capture_queue = queue.Queue()
        self._worker = None
        self._runner = None
        self._model_metadata = None
        self._model_lock = threading.Lock()
        # Detection state and the frame counters are touched by both the OctoPrint event
        # thread and the capture worker.
        self._state_lock = threading.Lock()
        self._frame_index = 0
        self._uploaded_frames = 0
        self._server_error_notified = False
        self._last_capture = None
        # Rolling snapshot refreshed only while the printer is still actually extruding (see
        # on_gcode_queuing). Slicer end-gcode stops extruding before any "present the print" move,
        # so this takes priority over ``_last_capture`` for the print-done ntfy — no gcode editing
        # required from the user.
        self._last_extrusion_capture = None
        self._last_extrusion_capture_at = 0.0
        # Plugin messages are one-shot broadcasts lost if no browser tab is connected when they
        # fire (e.g. after a headless restart), so the last one is cached for on_api_get.
        self._last_notice = None
        self._last_notice_level = "info"
        self._last_status = None
        # Real-time capture loop: self-driven webcam snapshots instead of waiting on timelapse
        # CAPTURE_DONE events. Only used in local inference mode.
        self._realtime_thread = None
        self._realtime_stop_event = threading.Event()

    # ------------------------------------------------------------------ settings

    def get_settings_defaults(self):
        return {
            "enabled": True,
            "inference_mode": MODE_LOCAL,
            "request_timeout_seconds": 10,
            "threshold": 0.8,
            "required_failed_frames": 3,
            "consecutive_failed_frames": 3,
            "model_auto_update": True,
            "tflite_num_threads": 1,
            # 0 means "use the crop fraction reported by the inference server".
            "crop_fraction": 0,
            "upload_enabled": True,
            "upload_every_nth_frame": 5,
            "upload_uncertainty_margin": 0.2,
            "realtime_enabled": False,
            "realtime_min_interval_seconds": 1,
            "realtime_run_when_idle": False,
            "ntfy_enabled": False,
            "ntfy_server_url": "https://ntfy.sh",
            "ntfy_topic": "",
            "ntfy_token": "",
            "ntfy_notify_on_done": True,
        }

    def get_settings_restricted_paths(self):
        # Keep sensitive tokens out of the settings payload sent to non-admin users.
        return {"admin": [["ntfy_token"]]}

    def get_template_configs(self):
        # custom_bindings defaults to True in OctoPrint; must be set False explicitly so core
        # binds this template's data-bind attributes to the SettingsViewModel. Without it the
        # fields render unbound (ko.dataFor == undefined) and Save persists nothing.
        return [{"type": "settings", "custom_bindings": False}]

    def get_assets(self):
        return {
            "js": ["js/printsheriff.js"],
            "css": ["css/printsheriff.css"],
        }

    @property
    def inference_mode(self):
        # Remote mode is disabled in the UI for now; always run locally regardless of any
        # stored setting (e.g. from a config.yaml predating this change).
        return MODE_LOCAL

    @property
    def threshold(self):
        return float(self._settings.get(["threshold"]))

    @property
    def required_failed_frames(self):
        val = self._settings.get(["required_failed_frames"])
        if val is None:
            val = self._settings.get(["consecutive_failed_frames"])
        return max(1, int(val or 3))

    @property
    def window_size(self):
        # Slack (frames beyond M that are still allowed to be "good") scales with M itself:
        # a strict M=3 only needs a couple of tolerance frames, but a lenient M=20 should get
        # more absolute slack too, not the same fixed +2 - otherwise larger M becomes stricter
        # in relative terms. Always keep at least 2 frames of tolerance for occasional flips.
        required = self.required_failed_frames
        slack = max(2, math.ceil(required / 2))
        return required + slack

    @property
    def server_url(self):
        return DEFAULT_SERVER_URL

    @property
    def realtime_min_interval_seconds(self):
        configured = float(self._settings.get(["realtime_min_interval_seconds"]) or 0)
        return max(MIN_REALTIME_INTERVAL_SECONDS, configured)

    @property
    def _realtime_configured(self):
        # Real-time capture bypasses OctoPrint's timelapse entirely, so it only makes sense for
        # local (on-device) inference where there is no per-request network round trip.
        return (
            self._settings.get_boolean(["enabled"])
            and self.inference_mode == MODE_LOCAL
            and self._settings.get_boolean(["realtime_enabled"])
        )

    def _realtime_should_run(self):
        if not self._realtime_configured:
            return False
        # Normally real-time capture tracks the print job like timelapse would; this setting
        # opts out of that so the loop keeps watching the bed even between/before prints.
        if self._settings.get_boolean(["realtime_run_when_idle"]):
            return True
        return self._printer.is_printing()

    @property
    def crop_fraction(self):
        configured = float(self._settings.get(["crop_fraction"]) or 0)
        if configured > 0:
            return min(1.0, max(0.1, configured))
        with self._model_lock:
            metadata = self._model_metadata
        if metadata:
            return float(metadata.get("crop_fraction", 1.0))
        return 1.0

    def _client(self):
        timeout = max(1, int(self._settings.get(["request_timeout_seconds"])))
        return InferenceServerClient(self.server_url, timeout)

    # ------------------------------------------------------------------ lifecycle

    def on_after_startup(self):
        self._worker = threading.Thread(
            target=self._process_captures,
            name=self._identifier,
            daemon=True,
        )
        self._worker.start()
        if self.inference_mode == MODE_LOCAL:
            self._refresh_model_async()
        else:
            self._check_server_connection_async()
        if self._realtime_should_run():
            self._start_realtime()

    def on_shutdown(self):
        self._stop_realtime()
        self._capture_queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout=5)

    def on_settings_save(self, data):
        super(PrintSheriffPlugin, self).on_settings_save(data)
        self._logger.info("Settings saved (incoming keys: %s)", sorted((data or {}).keys()))
        with self._state_lock:
            self._detection.reset()
        if self.inference_mode == MODE_LOCAL:
            self._refresh_model_async()
        else:
            self._release_model()
            self._check_server_connection_async()
        if self._realtime_should_run():
            self._start_realtime()
        else:
            self._stop_realtime()

    def _release_model(self):
        with self._model_lock:
            self._runner = None
            self._model_metadata = None

    # ------------------------------------------------------------------ events

    def on_event(self, event, payload):
        if event == Events.PRINT_STARTED:
            with self._state_lock:
                self._detection.reset()
                self._frame_index = 0
                self._uploaded_frames = 0
                self._server_error_notified = False
                self._last_capture = None
                self._last_extrusion_capture = None
                self._last_extrusion_capture_at = 0.0
            if self.inference_mode == MODE_LOCAL:
                self._refresh_model_async()
            else:
                self._check_server_connection_async()
            if self._realtime_should_run():
                self._start_realtime()
            return

        if event in (Events.PRINT_DONE, Events.PRINT_FAILED, Events.PRINT_CANCELLED):
            if not self._realtime_should_run():
                self._stop_realtime()
            with self._state_lock:
                self._detection.reset()
            if event == Events.PRINT_DONE and self._settings.get_boolean(["ntfy_notify_on_done"]):
                self._send_print_done_ntfy_async(payload or {})
            return

        if event != Events.CAPTURE_DONE or not self._settings.get_boolean(["enabled"]):
            return

        if self._realtime_configured:
            # The real-time loop drives its own captures instead of OctoPrint's timelapse.
            return

        image_path = self._capture_path(payload or {})
        if image_path is None:
            self._logger.debug("Skipping timelapse capture without a readable image path: %r", payload)
            return
        self._capture_queue.put(image_path)

    def _capture_path(self, payload):
        for key in ("file", "path", "image"):
            value = payload.get(key)
            if isinstance(value, str) and os.path.isfile(value):
                return value
        return None

    # ------------------------------------------------------------------ model handling

    def _refresh_model_async(self):
        # Never block startup or a print on a network round trip.
        threading.Thread(
            target=self._refresh_model,
            name=self._identifier + "-model-sync",
            daemon=True,
        ).start()

    def _refresh_model(self):
        sync = ModelSync(self.get_plugin_data_folder())
        metadata = sync.load_cached()
        changed = False

        if metadata is None or self._settings.get_boolean(["model_auto_update"]):
            try:
                metadata, changed = sync.sync(self._client())
            except Exception as error:
                self._logger.warning("Could not sync the model from the inference server: %s", error)
                metadata = sync.load_cached()

        if metadata is None:
            self._mark_model_unavailable(
                "No local model yet. Could not download it from {0}.".format(DEFAULT_SERVER_URL)
            )
            return

        with self._model_lock:
            if self._runner is not None and not changed:
                self._model_metadata = metadata
                return
            try:
                from .tflite_runner import TFLiteRunner

                self._runner = TFLiteRunner(
                    sync.model_path, self._settings.get_int(["tflite_num_threads"])
                )
                inference_ms = self._measure_inference_ms(self._runner)
            except Exception as error:
                self._runner = None
                self._logger.error("Could not load the local TFLite model: %s", error, exc_info=True)
                self._mark_model_unavailable("Local model could not be loaded: {0}".format(error))
                return
            self._model_metadata = metadata

        self._logger.info(
            "Local model ready (%s, %dx%d, crop %.2f) in %.0f ms per inference",
            (metadata.get("sha256") or "")[:12],
            self._runner.input_height,
            self._runner.input_width,
            self.crop_fraction,
            inference_ms,
        )
        message = "Local model ready ({0}, {1}x{2}). One inference took {3:.0f} ms.".format(
            (metadata.get("sha256") or "unknown")[:12],
            self._runner.input_width,
            self._runner.input_height,
            inference_ms,
        )
        if inference_ms > SLOW_INFERENCE_MS:
            self._send_notice(
                message + " That is slow on this host; switch to server inference for faster results.",
                level="warning",
            )
        else:
            self._send_notice(message, level="success")

    def _measure_inference_ms(self, runner):
        """Time one inference on a blank frame so the cost is known before the first capture.

        The very first invoke on a freshly loaded interpreter pays one-off setup costs (buffer
        allocation, thread pool spin-up) that don't recur on later frames, so it would overstate
        steady-state latency. Run one untimed warm-up call first, then time a second call.
        """
        import numpy as np

        pixels = np.zeros((runner.input_height, runner.input_width, 3), dtype=np.uint8)
        runner.predict(pixels)
        start = time.perf_counter()
        runner.predict(pixels)
        return (time.perf_counter() - start) * 1000

    def _mark_model_unavailable(self, message):
        self._logger.warning(message)
        self._send_notice(message + " Detection is paused until a model is available.")

    def _check_server_connection_async(self):
        threading.Thread(
            target=self._check_server_connection,
            name=self._identifier + "-server-check",
            daemon=True,
        ).start()

    def _check_server_connection(self):
        if not self.server_url:
            self._send_notice("Set the inference server URL in the PrintSheriff settings.")
            return
        try:
            self._client().check_health()
        except Exception as error:
            self._logger.warning("Could not reach inference server at %s: %s", self.server_url, error)
            self._send_notice("Could not reach inference server at {0}: {1}".format(self.server_url, error))
            return
        self._logger.info("Connected to inference server at %s", self.server_url)
        self._send_notice("Connected to inference server at {0}".format(self.server_url))

    # ------------------------------------------------------------------ real-time capture

    def _start_realtime(self):
        if self._realtime_thread is not None and self._realtime_thread.is_alive():
            return
        self._realtime_stop_event.clear()
        self._realtime_thread = threading.Thread(
            target=self._realtime_loop,
            name=self._identifier + "-realtime",
            daemon=True,
        )
        self._realtime_thread.start()
        self._logger.info(
            "Real-time local inference started (minimum interval %.1fs)",
            self.realtime_min_interval_seconds,
        )

    def _stop_realtime(self):
        self._realtime_stop_event.set()
        thread, self._realtime_thread = self._realtime_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    def _realtime_loop(self):
        while not self._realtime_stop_event.is_set():
            started_at = time.perf_counter()
            try:
                image_bytes = self._capture_webcam_snapshot()
            except Exception as error:
                image_bytes = None
                self._logger.warning("Real-time webcam capture failed: %s", error)
            if image_bytes:
                try:
                    self._evaluate_capture_bytes(image_bytes, "realtime-capture.jpg")
                except Exception:
                    self._logger.exception("Could not evaluate real-time capture")
            # Wait out whatever is left of the minimum interval, then start the next capture
            # right away — i.e. one inference finishes before the next capture begins.
            elapsed = time.perf_counter() - started_at
            remaining = self.realtime_min_interval_seconds - elapsed
            if remaining > 0:
                self._realtime_stop_event.wait(remaining)

    def _capture_webcam_snapshot(self):
        # Imported lazily: older OctoPrint versions (<1.9) do not ship this module, and the
        # real-time feature should degrade to a clear error rather than break plugin import.
        from octoprint.webcams import get_snapshot_webcam

        webcam = get_snapshot_webcam()
        if webcam is None:
            raise RuntimeError("No snapshot-capable webcam is configured in OctoPrint")
        chunks = webcam.providerPlugin.take_webcam_snapshot(webcam.config.name)
        return b"".join(chunk for chunk in chunks if chunk)

    # ------------------------------------------------------------------ gcode hooks

    def on_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, subcode=None, tags=None, *args, **kwargs):
        """Refresh the last-extrusion snapshot; never modifies the gcode stream.

        No slicer configuration needed: extrusion stops before any end-gcode "present the print"
        move, so a snapshot that only refreshes on real extrusion moves naturally freezes on a
        frame from right around when printing actually finished.
        """
        if gcode not in ("G0", "G1", "G2", "G3") or not _EXTRUSION_MOVE_RE.search(cmd):
            return None
        self._maybe_capture_last_extrusion_frame()
        return None

    def _maybe_capture_last_extrusion_frame(self):
        now = time.perf_counter()
        with self._state_lock:
            if now - self._last_extrusion_capture_at < LAST_EXTRUSION_CAPTURE_MIN_INTERVAL_SECONDS:
                return
            self._last_extrusion_capture_at = now
        threading.Thread(
            target=self._capture_last_extrusion_frame,
            name=self._identifier + "-extrusion-capture",
            daemon=True,
        ).start()

    def _capture_last_extrusion_frame(self):
        try:
            image_bytes = self._capture_webcam_snapshot()
        except Exception as error:
            # Very frequent while printing without a configured webcam, so debug not warning.
            self._logger.debug("Skipping last-extrusion webcam capture: %s", error)
            return
        with self._state_lock:
            self._last_extrusion_capture = (image_bytes, "print-finish-capture.jpg")

    # ------------------------------------------------------------------ processing

    def _process_captures(self):
        while True:
            image_path = self._capture_queue.get()
            try:
                if image_path is None:
                    return
                self._evaluate_capture(image_path)
            except Exception:
                self._logger.exception("Could not evaluate timelapse capture: %s", image_path)
            finally:
                self._capture_queue.task_done()

    def _evaluate_capture(self, image_path):
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()
        self._evaluate_capture_bytes(image_bytes, os.path.basename(image_path))

    def _evaluate_capture_bytes(self, image_bytes, image_name):
        with self._state_lock:
            self._last_capture = (image_bytes, image_name)

        probability, inference_ms = self._predict(image_bytes)

        if probability is not None:
            required = self.required_failed_frames
            window = self.window_size
            with self._state_lock:
                failed_frames, current_window_size, triggered = self._detection.update(
                    probability, self.threshold, required, window
                )
            self._send_status(
                probability, failed_frames, current_window_size, triggered, inference_ms
            )
            if triggered:
                self._send_alert(probability, required, current_window_size)
                self._send_failure_ntfy_async(
                    probability,
                    required,
                    current_window_size,
                    image_bytes,
                    image_name,
                )

        if self.inference_mode == MODE_LOCAL:
            self._collect(image_bytes, probability)

    def _predict(self, image_bytes):
        """Return ``(probability, inference_ms)``; probability is ``None`` when unavailable.

        ``inference_ms`` is only measured for local inference and is ``None`` otherwise.
        """
        if self.inference_mode == MODE_SERVER:
            try:
                return self._client().predict(image_bytes), None
            except Exception as error:
                self._logger.warning("Inference server prediction failed: %s", error)
                with self._state_lock:
                    already_notified = self._server_error_notified
                    self._server_error_notified = True
                if not already_notified:
                    self._send_notice("Inference server prediction failed: {0}".format(error))
                return None, None

        with self._model_lock:
            runner = self._runner
        if runner is None:
            return None, None

        pixels = preprocess_rgb_bytes(
            image_bytes, runner.input_height, runner.input_width, self.crop_fraction
        )
        start = time.perf_counter()
        probability = runner.predict(pixels)
        inference_ms = (time.perf_counter() - start) * 1000
        return probability, inference_ms

    def _collect(self, image_bytes, probability):
        """Upload selected frames so they can be labelled and used for retraining."""
        with self._state_lock:
            frame_index = self._frame_index
            self._frame_index += 1

        if not self._settings.get_boolean(["upload_enabled"]) or not self.server_url:
            return

        selected = should_upload(
            frame_index,
            probability,
            self.threshold,
            self._settings.get_int(["upload_every_nth_frame"]),
            float(self._settings.get(["upload_uncertainty_margin"])),
        )
        if not selected:
            return

        try:
            self._client().collect(image_bytes, probability, source="local_plugin")
        except Exception as error:
            # Collection is best effort: never let it interfere with failure detection.
            self._logger.warning("Could not upload capture for training: %s", error)
            return

        with self._state_lock:
            self._uploaded_frames += 1

    # ------------------------------------------------------------------ messaging

    def _send_alert(self, probability, required, window):
        message = (
            "Possible spaghetti failure: {0:.1%} failed probability ({1} of last {2} frames failed)."
        ).format(probability, required, window)
        self._logger.warning(message)
        self._plugin_manager.send_plugin_message(
            self._identifier,
            {"type": "failure_alert", "message": message, "probability": probability},
        )
        if self._printer.is_operational():
            self._printer.commands(["M117 Spaghetti detected"])

    def _send_failure_ntfy_async(self, probability, required, window, image_bytes, image_name):
        title = "PrintSheriff: possible print failure ({0:.1%})".format(probability)
        message = "Failed probability: {0:.1%} ({1} of last {2} frames failed).".format(
            probability, required, window
        )
        self._send_ntfy_async(
            title,
            message,
            image_bytes=image_bytes,
            image_name=image_name,
        )

    def _send_print_done_ntfy_async(self, payload):
        if not self._settings.get_boolean(["ntfy_enabled"]):
            return
        threading.Thread(
            target=self._send_print_done_ntfy,
            args=(payload,),
            name=self._identifier + "-ntfy-done",
            daemon=True,
        ).start()

    def _send_print_done_ntfy(self, payload):
        self._capture_queue.join()
        with self._state_lock:
            # The last-extrusion capture predates the presentation move, so prefer it.
            last_capture = self._last_extrusion_capture or self._last_capture
            self._last_extrusion_capture = None

        name = payload.get("name") or payload.get("file") or "Print job"
        message = "{0} finished successfully.".format(os.path.basename(str(name)))
        elapsed = payload.get("time")
        if elapsed:
            message += " Took {0}.".format(self._format_duration(elapsed))
        image_bytes, image_name = last_capture or (None, "capture.jpg")
        self._send_ntfy(
            "PrintSheriff: print finished",
            message,
            image_bytes,
            image_name,
            priority="default",
            tags="white_check_mark,3d_printer",
        )

    @staticmethod
    def _format_duration(seconds):
        total = int(float(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return "{0}h {1}m".format(hours, minutes)
        if minutes:
            return "{0}m {1}s".format(minutes, secs)
        return "{0}s".format(secs)

    def _send_ntfy_async(
        self,
        title,
        message,
        image_bytes=None,
        image_name="capture.jpg",
        priority="high",
        tags="warning,3d_printer",
    ):
        if not self._settings.get_boolean(["ntfy_enabled"]):
            return
        threading.Thread(
            target=self._send_ntfy,
            args=(title, message, image_bytes, image_name, priority, tags),
            name=self._identifier + "-ntfy",
            daemon=True,
        ).start()

    def _send_ntfy(self, title, message, image_bytes, image_name, priority, tags):
        server_url = (self._settings.get(["ntfy_server_url"]) or "https://ntfy.sh").strip()
        topic = (self._settings.get(["ntfy_topic"]) or "").strip()
        token = (self._settings.get(["ntfy_token"]) or "").strip() or None

        if not topic:
            self._send_notice(
                "ntfy alert skipped: set the ntfy topic in PrintSheriff settings.",
                level="warning",
            )
            return

        try:
            with_attachment = send_ntfy_notification(
                server_url=server_url,
                topic=topic,
                title=title,
                message=message,
                image_bytes=image_bytes,
                filename=image_name,
                priority=priority,
                tags=tags,
                token=token,
                timeout=max(1, int(self._settings.get(["request_timeout_seconds"]))),
            )
        except Exception as error:
            self._logger.warning("Could not send ntfy notification: %s", error)
            self._send_notice("Could not send ntfy notification: {0}".format(error), level="warning")
            return

        if image_bytes and not with_attachment:
            self._logger.warning(
                "ntfy notification sent to topic '%s' without the capture: the server rejected "
                "the attachment (enable attachment-cache-dir and base-url on the ntfy server).",
                topic,
            )
            self._send_notice(
                "ntfy notification sent without the capture image: the ntfy server has "
                "attachments disabled.",
                level="warning",
            )
            return

        self._logger.info("ntfy notification sent to topic '%s'", topic)
        self._send_notice("ntfy notification sent to topic '{0}'.".format(topic))

    def _send_status(
        self, probability, failed_frames, window_size, alerted, inference_ms=None
    ):
        payload = {
            "type": "status",
            "probability": probability,
            "failed_frames": failed_frames,
            "window_size": window_size,
            "consecutive_failed_frames": failed_frames,
            "alerted": alerted,
            "inference_mode": self.inference_mode,
        }
        if self.inference_mode == MODE_LOCAL:
            with self._model_lock:
                metadata = self._model_metadata or {}
                model_available = self._runner is not None
            with self._state_lock:
                uploaded_frames = self._uploaded_frames
            payload.update(
                {
                    "model_available": model_available,
                    "model_sha256_short": (metadata.get("sha256") or "")[:12],
                    "uploaded_frames": uploaded_frames,
                    "inference_ms": inference_ms,
                }
            )
        self._last_status = payload
        self._plugin_manager.send_plugin_message(self._identifier, payload)

    def _send_notice(self, message, level="info"):
        self._last_notice = message
        self._last_notice_level = level
        self._plugin_manager.send_plugin_message(
            self._identifier, {"type": "notice", "message": message, "level": level}
        )

    # ------------------------------------------------------------------ simple api

    def is_api_protected(self):
        return True

    def on_api_get(self, request):
        return flask.jsonify(
            {
                "notice": self._last_notice,
                "notice_level": self._last_notice_level,
                "status": self._last_status,
            }
        )


__plugin_name__ = "PrintSheriff"
__plugin_version__ = "0.1.0"
__plugin_description__ = (
    "Detect 3D-print failures in timelapse captures with a neural network running locally on "
    "the printer host."
)
__plugin_author__ = "xeonqq"
__plugin_license__ = "AGPLv3"
__plugin_url__ = "https://github.com/xeonqq/octoprint_printsheriff"
__plugin_privacypolicy__ = (
    "https://github.com/xeonqq/octoprint_printsheriff/blob/master/PRIVACY.md"
)
__plugin_pythoncompat__ = ">=3.9,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PrintSheriffPlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.on_gcode_queuing,
    }