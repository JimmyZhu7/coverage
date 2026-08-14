// Coverage's service worker — deadline push alerts only.
//
// Deliberately NOT an offline-cache worker. A stale-cache bug on a live
// deadline feed (the wrong "closes in 2 days" served from cache after the
// real page moved on) would be actively harmful, and that risk buys this
// worker nothing it needs: showing a notification and focusing a window on
// click require no cached response at all. Caching/offline support is a
// different feature with a different risk profile — out of scope here, on
// purpose. Keep this file to exactly those two listeners.
//
// Served from /static/ (see templates/base.html's registration script), so
// its default scope is /static/ rather than the whole origin. That's fine:
// push events fire on a registered worker regardless of which page (if any)
// it currently controls, and `clients.openWindow` below can open any
// same-origin URL regardless of scope — neither capability this worker uses
// depends on controlling app pages.

self.addEventListener("push", function (event) {
  var data = { title: "Coverage", body: "A tracked role is closing soon.", url: "/opportunities/mine/" };
  if (event.data) {
    try {
      data = Object.assign(data, event.data.json());
    } catch (e) {
      // Not JSON (shouldn't happen — accounts.push always sends JSON) —
      // fall back to the defaults above rather than throwing and dropping
      // the notification entirely.
    }
  }
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      // Same mark the installed app icon uses (manifest.webmanifest) — a
      // notification with an unrelated or missing icon is the fastest way
      // to look like spam.
      icon: "/static/img/icon-192.png",
      badge: "/static/img/icon-192.png",
      data: { url: data.url || "/opportunities/mine/" },
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || "/opportunities/mine/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (list) {
      // Focus an already-open Coverage tab rather than piling up a new one
      // every time a deadline fires — the same instinct as any native app's
      // notification tap.
      for (var i = 0; i < list.length; i++) {
        var client = list[i];
        if ("focus" in client) {
          if ("navigate" in client && client.url !== url) client.navigate(url);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
