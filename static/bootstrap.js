"use strict";
// A file preview has no API server. Do not start storage, requests or polling.
if (location.protocol === "file:") {
  document.getElementById("chat-app").hidden = true;
  document.getElementById("file-help").hidden = false;
} else {
  const script = document.createElement("script");
  script.src = "./chat.js";
  document.head.append(script);
}
