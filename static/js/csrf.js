/*
 * CSRF と JSON リクエストの共通ヘルパー（design/07-security.md §8 CSRF）。
 * `__Host-csrf` cookie を読み、非 GET に `X-CSRF-Token` を付ける。値は URL に載せない。
 */
(function () {
  "use strict";

  function readCookie(name) {
    var prefix = name + "=";
    var parts = document.cookie ? document.cookie.split("; ") : [];
    for (var i = 0; i < parts.length; i += 1) {
      if (parts[i].indexOf(prefix) === 0) {
        return decodeURIComponent(parts[i].slice(prefix.length));
      }
    }
    return "";
  }

  function csrfToken() {
    return readCookie("__Host-csrf");
  }

  async function requestJson(path, options) {
    var settings = options || {};
    var method = settings.method || "GET";
    var headers = settings.headers || {};
    if (method !== "GET") {
      headers["X-CSRF-Token"] = csrfToken();
      headers["Content-Type"] = "application/json";
    }
    return fetch(path, {
      method: method,
      headers: headers,
      body: settings.body,
      credentials: "same-origin"
    });
  }

  async function errorMessage(response) {
    try {
      var body = await response.json();
      return body.error && body.error.message ? body.error.message : "";
    } catch (error) {
      return "";
    }
  }

  window.AP = window.AP || {};
  window.AP.readCookie = readCookie;
  window.AP.csrfToken = csrfToken;
  window.AP.requestJson = requestJson;
  window.AP.errorMessage = errorMessage;
})();
