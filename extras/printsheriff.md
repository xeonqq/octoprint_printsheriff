---
layout: plugin

id: printsheriff
title: PrintSheriff
description: Detect 3D-print failures from OctoPrint timelapse captures with local TensorFlow Lite inference and optional ntfy alerts
authors:
- xeonqq
license: AGPLv3

date: 2026-09-24

homepage: https://github.com/xeonqq/octoprint_printsheriff
source: https://github.com/xeonqq/octoprint_printsheriff
archive: https://github.com/xeonqq/octoprint_printsheriff/archive/master.zip
privacypolicy: https://github.com/xeonqq/octoprint_printsheriff/blob/master/PRIVACY.md

tags:
- monitoring
- notifications
- print failure detection
- timelapse
- machine learning
- tensorflow lite

# Add screenshots and a featured image before opening the registration pull request.
# screenshots:
# - url: /assets/img/plugins/printsheriff/overview.png
#   alt: PrintSheriff settings and detection status in OctoPrint
#   caption: PrintSheriff settings and current detection status
# featuredimage: /assets/img/plugins/printsheriff/overview.png

compatibility:
  octoprint:
  - 1.8.0
  os:
  - linux
  python: ">=3.9,<4"

attributes:
- cloud
- ai-developed

---

PrintSheriff evaluates OctoPrint timelapse captures for signs of a failed 3D print. Inference
runs locally on the OctoPrint host with TensorFlow Lite. When a configurable number of failed
predictions is reached, the plugin raises a persistent OctoPrint warning and can send an
optional ntfy notification with the capture attached.

The plugin can download and cache updated models from `https://api.printsheriff.com`. It can
also upload selected frames for model improvement when **Help improve the model** is enabled
(opt-in, disabled by default); this setting can be toggled in the setup wizard or plugin
settings. See the
[privacy policy](https://github.com/xeonqq/octoprint_printsheriff/blob/master/PRIVACY.md) for
data-handling details.

Timelapse must be enabled in OctoPrint. PrintSheriff does not automatically pause or cancel
prints. See the [project README](https://github.com/xeonqq/octoprint_printsheriff#readme) for
installation, configuration, and hardware requirements.
