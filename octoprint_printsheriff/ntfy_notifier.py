"""Send failure-alert notifications with the offending capture attached via ntfy."""

from __future__ import absolute_import

import os
import urllib.error
import urllib.parse
import urllib.request


def send_ntfy_notification(
    server_url,
    topic,
    title,
    message,
    image_bytes=None,
    filename="capture.jpg",
    priority="high",
    tags="warning,3d_printer",
    token=None,
    timeout=10,
):
    """Publish a push notification to an ntfy server.

    If ``image_bytes`` is provided, the image is posted as a file attachment along with
    HTTP headers for Title, Message, Priority, and Tags. Servers that have attachments
    disabled fall back to a text-only notification.

    Returns ``True`` when the notification was delivered with its attachment intact.
    """
    if not server_url or not topic:
        raise ValueError("ntfy server URL and topic must be specified")

    base_url = (server_url or "https://ntfy.sh").rstrip("/")
    clean_topic = topic.strip().lstrip("/")
    target_url = "{0}/{1}".format(base_url, clean_topic)

    headers = {
        "Title": title.encode("ascii", errors="replace").decode("ascii"),
        "Message": message.encode("ascii", errors="replace").decode("ascii"),
        "Priority": str(priority),
        "Tags": str(tags),
    }

    if token and str(token).strip():
        headers["Authorization"] = "Bearer {0}".format(str(token).strip())

    if image_bytes:
        attachment_headers = dict(headers)
        attachment_headers["Filename"] = filename
        attachment_headers["Content-Type"] = (
            "image/png" if filename.lower().endswith(".png") else "image/jpeg"
        )
        try:
            _post(target_url, image_bytes, attachment_headers, timeout)
            return True
        except RuntimeError:
            # A server with attachments disabled answers 400 mid-upload and drops the
            # connection, so this usually surfaces as a reset rather than a clean status.
            pass

    headers["Content-Type"] = "text/plain; charset=utf-8"
    _post(target_url, message.encode("utf-8"), headers, timeout)
    return not image_bytes


def _post(target_url, data, headers, timeout):
    req = urllib.request.Request(target_url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError("ntfy server returned HTTP {0}: {1}".format(error.code, error.reason))
    except urllib.error.URLError as error:
        if "WRONG_VERSION_NUMBER" in str(error.reason).upper():
            raise RuntimeError(
                "Could not connect to ntfy server: the configured HTTPS endpoint is serving "
                "plain HTTP. Use http:// for that server/port, or configure TLS and use its "
                "HTTPS port."
            )
        raise RuntimeError("Could not connect to ntfy server: {0}".format(error.reason))
    except OSError as error:
        raise RuntimeError("Could not connect to ntfy server: {0}".format(error))
