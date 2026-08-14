// Settings' Notifications toggle: register the service worker, ask for
// permission, subscribe via PushManager, POST the subscription to the
// server. Kept out of settings.html itself (a high-traffic file this
// session) so the toggle stays a small, isolated, easily-relocatable block —
// one <script src> line plus the markup it wires up.
//
// Every failure mode below (unsupported browser, permission denied, no
// VAPID key configured) resolves to a plain "unavailable" state, never a
// broken button — see coverage-copy-style: no dead ends.
(function () {
  "use strict";

  // web-push's standard base64url -> Uint8Array conversion, needed because
  // PushManager.subscribe() wants applicationServerKey as a byte array, not
  // the base64url string VAPID_PUBLIC_KEY is stored/printed as.
  function urlBase64ToUint8Array(base64) {
    var padding = "=".repeat((4 - (base64.length % 4)) % 4);
    var b64 = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(b64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function init() {
    var root = document.querySelector("[data-push-root]");
    if (!root) return;

    function post(url, body) {
      return fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": root.dataset.csrf },
        body: JSON.stringify(body || {}),
      });
    }

    var toggle = root.querySelector("[data-push-toggle]");
    var status = root.querySelector("[data-push-status]");
    if (!toggle) return;

    var vapidKey = root.dataset.vapidPublicKey || "";
    var supported = "serviceWorker" in navigator && "PushManager" in window;

    function setStatus(text) {
      if (status) status.textContent = text;
    }

    function unavailable(text) {
      toggle.disabled = true;
      setStatus(text);
    }

    if (!vapidKey) {
      unavailable("Not set up yet.");
      return;
    }
    if (!supported) {
      unavailable("Not supported in this browser.");
      return;
    }
    if (window.Notification && Notification.permission === "denied") {
      unavailable("Blocked. Enable notifications for this site in your browser settings.");
      return;
    }

    function subscribe() {
      toggle.disabled = true;
      setStatus("Turning on…");
      navigator.serviceWorker
        .register("/static/service-worker.js")
        .then(function (reg) {
          return reg.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(vapidKey),
          });
        })
        .then(function (sub) {
          return post(root.dataset.subscribeUrl, sub.toJSON());
        })
        .then(function (r) {
          if (!r.ok) throw new Error("save failed");
          toggle.checked = true;
          setStatus("On. You'll get an alert when a tracked role is closing soon.");
        })
        .catch(function () {
          toggle.checked = false;
          setStatus("Couldn't turn this on. Try again in a moment.");
        })
        .finally(function () {
          toggle.disabled = false;
        });
    }

    function unsubscribe() {
      toggle.disabled = true;
      setStatus("Turning off…");
      navigator.serviceWorker
        .getRegistration("/static/service-worker.js")
        .then(function (reg) {
          return reg ? reg.pushManager.getSubscription() : null;
        })
        .then(function (sub) {
          if (!sub) return null;
          var endpoint = sub.endpoint;
          return sub.unsubscribe().then(function () {
            return post(root.dataset.unsubscribeUrl, { endpoint: endpoint });
          });
        })
        .then(function () {
          toggle.checked = false;
          setStatus("Off.");
        })
        .catch(function () {
          setStatus("Couldn't turn this off. Try again in a moment.");
        })
        .finally(function () {
          toggle.disabled = false;
        });
    }

    toggle.addEventListener("change", function () {
      if (toggle.checked) {
        if (window.Notification && Notification.permission === "default") {
          Notification.requestPermission().then(function (perm) {
            if (perm === "granted") subscribe();
            else {
              toggle.checked = false;
              setStatus(perm === "denied" ? "Blocked. Enable notifications for this site in your browser settings." : "Not turned on.");
            }
          });
        } else {
          subscribe();
        }
      } else {
        unsubscribe();
      }
    });
  }

  if (document.readyState !== "loading") init();
  else document.addEventListener("DOMContentLoaded", init);
})();
