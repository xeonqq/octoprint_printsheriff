# PrintSheriff Privacy Policy

Last updated: 2026-09-24

PrintSheriff performs print-failure detection locally on the OctoPrint host. It does not
send images for inference by default: the locally cached TensorFlow Lite model is used for
classification.

## Model service

When model auto-update is enabled, PrintSheriff connects over HTTPS to
`api.printsheriff.com` to check model metadata and, when needed, download the model. The
model is cached locally after download. If the service is unavailable, the plugin keeps using
the cached model when possible and does not interrupt printing.

## Optional training-data uploads

The **Help improve the model** setting is disabled by default (opt-in). Users can choose to
enable it during initial setup in the wizard or in **Settings > PrintSheriff** to help train and
improve future failure-detection models. When enabled, PrintSheriff may send selected webcam or
timelapse JPEG frames to `api.printsheriff.com` over HTTPS for model improvement. Uploads include
the locally calculated failure probability and the source label `local_plugin`. Uploads are best
effort and are never required for print-failure detection.

Users can enable or disable training-data uploads at any time in **Settings > PrintSheriff**.
Uploaded images are retained by the service for up to 14 days and then deleted, according to
the service's retention process.

## ntfy notifications

If failure or completion notifications are enabled, PrintSheriff sends the configured
notification message and, when available, a captured image to the ntfy server configured by
the user. The default is `https://ntfy.sh`; users may configure a self-hosted HTTPS ntfy
server instead. An ntfy access token is stored in OctoPrint's protected plugin settings and
is sent only to the configured ntfy server.

## Local data

The plugin stores its cached model, model metadata, and temporary capture data in OctoPrint's
plugin data directory. OctoPrint's own settings and data-management controls apply to those
files.

## Contact

For privacy questions or requests concerning the PrintSheriff service, contact the project
maintainer through the project's issue tracker:

https://github.com/xeonqq/octoprint_printsheriff/issues

This policy may be updated when the service or plugin behavior changes.
