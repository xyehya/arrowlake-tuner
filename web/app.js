"use strict";
// Placeholder (G-103). The dashboard is G-105.
fetch("/api/status").then((r) => r.json()).then((s) => {
  document.getElementById("status").textContent = s.helper_alive ? "helper connected" : "helper not connected";
});
