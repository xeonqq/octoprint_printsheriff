"""TFLite interpreter wrapper shared by the plugin, the inference server and the tools."""

from __future__ import absolute_import

import platform
import threading

import numpy as np


def _interpreter_classes():
    """Return ``(Interpreter, OpResolverType, load_delegate)``, preferring the lightweight runtime.

    ``tflite-runtime`` has no wheel for every Python/architecture combination, so hosts that
    only have full TensorFlow installed fall back to ``tf.lite``.
    """
    try:
        import tflite_runtime.interpreter as runtime
    except ImportError:
        try:
            import tensorflow as tf
        except ImportError:
            raise RuntimeError(
                "Neither tflite-runtime nor tensorflow is installed; "
                "install one of them to enable local inference"
            )
        # tf.lite.Interpreter is a lazily resolved attribute, not an importable submodule.
        return tf.lite.Interpreter, getattr(tf.lite.experimental, "OpResolverType", None), tf.lite.experimental.load_delegate
    return runtime.Interpreter, getattr(runtime, "OpResolverType", None), runtime.load_delegate


def _edgetpu_library_name():
    system = platform.system()
    if system == "Linux":
        return "libedgetpu.so.1"
    if system == "Darwin":
        return "libedgetpu.1.dylib"
    if system == "Windows":
        return "edgetpu.dll"
    raise RuntimeError(f"Unsupported platform for the Edge TPU delegate: {system}")


def load_interpreter(model_path, num_threads, use_edgetpu=None):
    interpreter_class, op_resolver_type, load_delegate = _interpreter_classes()
    if use_edgetpu is None:
        # `*_edgetpu.tflite` is the naming convention used by the Edge TPU compiler.
        use_edgetpu = "edgetpu" in str(model_path).lower()

    kwargs = {"model_path": model_path, "num_threads": num_threads}
    if op_resolver_type is not None:
        # Disabling the default delegates keeps every caller numerically identical.
        kwargs["experimental_op_resolver_type"] = op_resolver_type.BUILTIN_WITHOUT_DEFAULT_DELEGATES
    if use_edgetpu:
        try:
            kwargs["experimental_delegates"] = [load_delegate(_edgetpu_library_name())]
        except (ValueError, OSError) as exc:
            # ValueError: the delegate library loaded but failed to initialize (e.g. no device
            # plugged in). OSError: the shared library itself is missing (libedgetpu not installed).
            raise RuntimeError(
                "Could not load the Edge TPU delegate; is a Coral Edge TPU connected and its "
                "runtime (libedgetpu) installed?"
            ) from exc

    interpreter = interpreter_class(**kwargs)
    interpreter.allocate_tensors()
    return interpreter


class TFLiteRunner(object):
    """Run a single-output binary classifier, handling input/output quantization."""

    def __init__(self, model_path, num_threads=1, use_edgetpu=None):
        self.model_path = model_path
        self._interpreter = load_interpreter(model_path, max(1, int(num_threads)), use_edgetpu)
        self._input_details = self._interpreter.get_input_details()[0]
        self._output_details = self._interpreter.get_output_details()[0]
        self._lock = threading.Lock()

        shape = self._input_details["shape"]
        self.input_height = int(shape[1])
        self.input_width = int(shape[2])

    @property
    def quantized(self):
        return self._input_details["dtype"] != np.float32

    def quantize_input(self, pixels):
        dtype = self._input_details["dtype"]
        if dtype == np.float32:
            return pixels.astype(np.float32)

        scale, zero_point = self._input_details["quantization"]
        if scale <= 0:
            raise RuntimeError("TFLite model has invalid input quantization parameters")
        info = np.iinfo(dtype)
        return np.clip(np.round(pixels / scale + zero_point), info.min, info.max).astype(dtype)

    def dequantize_output(self, output):
        if self._output_details["dtype"] == np.float32:
            return float(output)

        scale, zero_point = self._output_details["quantization"]
        if scale <= 0:
            raise RuntimeError("TFLite model has invalid output quantization parameters")
        return float((output - zero_point) * scale)

    def predict(self, pixels):
        """Return the failed probability for one preprocessed image."""
        model_input = np.expand_dims(self.quantize_input(pixels), axis=0)
        with self._lock:
            self._interpreter.set_tensor(self._input_details["index"], model_input)
            self._interpreter.invoke()
            output = self._interpreter.get_tensor(self._output_details["index"])[0][0]
        return self.dequantize_output(output)
