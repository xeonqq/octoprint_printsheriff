$(function () {
    function PrintSheriffViewModel(parameters) {
        var self = this;
        self.settings = parameters[0];
        self.overlay = null;
        self.overlayObserver = null;

        self.threshold = function () {
            try {
                var value = parseFloat(self.settings.settings.plugins.printsheriff.threshold());
                return isNaN(value) ? 0.5 : value;
            } catch (error) {
                return 0.5;
            }
        };

        self.COLLAPSE_STORAGE_KEY = "printSheriffOverlayCollapsed";

        self.isCollapsed = function () {
            try {
                return window.localStorage.getItem(self.COLLAPSE_STORAGE_KEY) === "1";
            } catch (error) {
                return false;
            }
        };

        // Folds the overlay down to just the rainbow meter bar so it stops occupying camera view.
        self.setCollapsed = function (collapsed) {
            if (!self.overlay) {
                return;
            }
            self.overlay.classList.toggle("printsheriff-overlay-collapsed", collapsed);
            self.overlay.setAttribute("aria-expanded", collapsed ? "false" : "true");
            self.overlay.title = collapsed ? "Click to expand PrintSheriff overlay" : "Click to fold PrintSheriff overlay";
            try {
                window.localStorage.setItem(self.COLLAPSE_STORAGE_KEY, collapsed ? "1" : "0");
            } catch (error) {
                // localStorage unavailable (e.g. private browsing) - collapse state just won't persist.
            }
        };

        self.toggleCollapsed = function () {
            self.setCollapsed(!self.overlay.classList.contains("printsheriff-overlay-collapsed"));
        };

        self.ensureOverlay = function () {
            if (self.overlay && document.body.contains(self.overlay)) {
                return;
            }
            self.overlay = null;

            var webcamImage = document.getElementById("webcam_image");
            if (!webcamImage) {
                return;
            }
            var container = webcamImage.parentElement;
            if (!container) {
                return;
            }
            if (window.getComputedStyle(container).position === "static") {
                container.style.position = "relative";
            }

            self.overlay = document.createElement("div");
            self.overlay.className = "printsheriff-overlay printsheriff-overlay-hidden";
            self.overlay.setAttribute("role", "button");
            self.overlay.setAttribute("tabindex", "0");
            self.overlay.setAttribute("aria-label", "PrintSheriff status overlay. Click or press Enter to fold or unfold.");
            self.overlay.textContent = "PrintSheriff: waiting for capture";
            self.overlay.addEventListener("click", self.toggleCollapsed);
            self.overlay.addEventListener("keydown", function (event) {
                if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
                    event.preventDefault();
                    self.toggleCollapsed();
                }
            });
            container.appendChild(self.overlay);
            self.setCollapsed(self.isCollapsed());
        };

        self.renderOverlay = function (state, symbol, headline, detail, probability) {
            self.ensureOverlay();
            if (!self.overlay) {
                return;
            }

            self.overlay.classList.remove(
                "printsheriff-overlay-hidden",
                "printsheriff-overlay-good",
                "printsheriff-overlay-warning",
                "printsheriff-overlay-alert",
                "printsheriff-overlay-unavailable"
            );
            self.overlay.classList.add("printsheriff-overlay-" + state);

            var symbolNode = document.createElement("span");
            symbolNode.className = "printsheriff-overlay-symbol";
            symbolNode.setAttribute("aria-hidden", "true");
            symbolNode.textContent = symbol;

            var headerNode = document.createElement("div");
            headerNode.className = "printsheriff-overlay-header";
            headerNode.appendChild(symbolNode);
            headerNode.appendChild(document.createTextNode(" " + headline));

            self.overlay.replaceChildren(headerNode);

            if (typeof probability === "number" && !isNaN(probability)) {
                var clampedProb = Math.max(0, Math.min(1, probability));
                var percentVal = Math.round(clampedProb * 100);
                var threshVal = Math.max(0, Math.min(100, Math.round(self.threshold() * 100)));

                var meterNode = document.createElement("div");
                meterNode.className = "printsheriff-meter";
                meterNode.setAttribute("role", "progressbar");
                meterNode.setAttribute("aria-valuenow", percentVal);
                meterNode.setAttribute("aria-valuemin", "0");
                meterNode.setAttribute("aria-valuemax", "100");
                meterNode.setAttribute("title", "Failure risk: " + percentVal + "% (Threshold: " + threshVal + "%)");

                var thresholdNode = document.createElement("div");
                thresholdNode.className = "printsheriff-meter-threshold";
                thresholdNode.style.left = threshVal + "%";
                thresholdNode.setAttribute("title", "Threshold: " + threshVal + "%");

                var markerNode = document.createElement("div");
                markerNode.className = "printsheriff-meter-marker";
                markerNode.style.left = percentVal + "%";

                meterNode.appendChild(thresholdNode);
                meterNode.appendChild(markerNode);
                self.overlay.appendChild(meterNode);
            }

            if (detail) {
                var detailNode = document.createElement("span");
                detailNode.className = "printsheriff-overlay-detail";
                detailNode.textContent = detail;
                self.overlay.appendChild(detailNode);
            }

            // Kept outside the collapsible content so folded state doesn't silence screen readers.
            var srStatusNode = document.createElement("span");
            srStatusNode.className = "printsheriff-sr-status sr-only";
            srStatusNode.setAttribute("aria-live", "polite");
            srStatusNode.textContent = detail ? headline + ". " + detail : headline;
            self.overlay.appendChild(srStatusNode);
        };

        self.onStartup = function () {
            self.ensureOverlay();
            self.overlayObserver = new MutationObserver(self.ensureOverlay);
            self.overlayObserver.observe(document.body, {childList: true, subtree: true});
        };

        self.noticeStyles = {
            success: {state: "good", symbol: "\u2713", pnotify: "success"},
            warning: {state: "warning", symbol: "\u26a0", pnotify: "warning"},
            info: {state: "unavailable", symbol: "\u2139", pnotify: "notice"}
        };

        self.renderNotice = function (message, level) {
            var style = self.noticeStyles[level] || self.noticeStyles.info;
            self.renderOverlay(style.state, style.symbol, "PrintSheriff", message);
            return style;
        };

        self.showNotice = function (message, level) {
            var style = self.renderNotice(message, level);
            new PNotify({
                title: style.symbol + " PrintSheriff",
                text: message,
                type: style.pnotify,
                hide: true,
                delay: 6000
            });
        };

        self.onAfterBinding = function () {
            // Live plugin messages are lost if no browser tab was open when they fired
            // (e.g. after a headless restart), so fetch whatever was last cached server-side.
            OctoPrint.simpleApiGet("printsheriff").done(function (response) {
                if (response.status) {
                    self.handleStatus(response.status);
                }
                if (response.notice) {
                    self.renderNotice(response.notice, response.notice_level);
                }
            });
        };

        self.handleStatus = function (data) {
            var percentage = (data.probability * 100).toFixed(1) + "%";
            var isFailed = data.probability >= self.threshold();

            var detailParts = [];
            if (isFailed) {
                if (typeof data.failed_frames === "number" && typeof data.window_size === "number") {
                    detailParts.push("Failed frames: " + data.failed_frames + "/" + data.window_size);
                } else {
                    detailParts.push("Consecutive failed frames: " + data.consecutive_failed_frames);
                }
            }
            if (data.inference_mode === "local") {
                detailParts.push("Local inference");
                if (data.model_available === false) {
                    detailParts.push("model unavailable");
                }
                if (typeof data.inference_ms === "number") {
                    detailParts.push(data.inference_ms.toFixed(0) + " ms");
                }
                if (typeof data.uploaded_frames === "number") {
                    detailParts.push("uploaded: " + data.uploaded_frames);
                }
            }
            var detail = detailParts.join(" \u00b7 ");

            if (data.alerted) {
                self.renderOverlay("alert", "\u2715", "FAILURE: " + percentage + " fail risk", detail, data.probability);
            } else if (isFailed) {
                self.renderOverlay("warning", "\u26a0", "FAILED: " + percentage + " fail risk", detail, data.probability);
            } else {
                self.renderOverlay("good", "\u2713", "OK (" + percentage + " fail risk)", detail, data.probability);
            }

            var status = "Failed probability: " + percentage;
            if (typeof data.failed_frames === "number" && typeof data.window_size === "number") {
                status += " | Failed frames: " + data.failed_frames + "/" + data.window_size;
            } else {
                status += " | Consecutive failed frames: " + data.consecutive_failed_frames;
            }
            new PNotify({
                title: (isFailed ? "\u26a0 " : "\u2713 ") + "PrintSheriff",
                text: status,
                type: isFailed ? "warning" : "notice",
                hide: true,
                delay: 2500
            });
        };

        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "printsheriff" || !data) {
                return;
            }

            if (data.type === "status") {
                self.handleStatus(data);
                return;
            }

            if (data.type === "failure_alert") {
                new PNotify({
                    title: "\u2715 PrintSheriff",
                    text: data.message,
                    type: "error",
                    hide: false
                });
                return;
            }

            if (data.type === "notice") {
                self.showNotice(data.message, data.level);
            }
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: PrintSheriffViewModel,
        dependencies: ["settingsViewModel"],
        elements: []
    });
});
