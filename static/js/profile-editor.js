/*
 * `GET /profile-editor` の編集画面（design/05-api-contract.md §6 / design/12-ux-secrets.md §13 /
 * design/07-security.md §8）。auth-guard.js が `GET /api/v1/me/profile` を probe して
 * 未ログインなら `/login` へ遷移する。取得した本文・状態を表示し、保存時に
 * `PATCH /api/v1/me/profile` を送る。本文は `value` / `textContent` で扱い、禁止 API を使わない。
 */
(function () {
  "use strict";

  function setStatus(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  document.addEventListener("DOMContentLoaded", async function () {
    var allowed = await window.AP.requireAuth();
    if (!allowed) {
      return;
    }
    var profile = await allowed.json();
    var form = document.getElementById("profile-form");
    var content = document.getElementById("content");
    var published = document.getElementById("published");
    var error = document.getElementById("form-error");
    var status = document.getElementById("form-status");
    var submitButton = form.querySelector("button[type=submit]");

    content.value = profile.content || "";
    published.checked = profile.status === "published";
    var currentStatus = profile.status;

    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      setStatus(error, "");
      setStatus(status, "");
      submitButton.disabled = true;

      try {
        var desiredStatus = published.checked ? "published" : "draft";
        var payload = { content: content.value };
        if (desiredStatus !== currentStatus) {
          payload.status = desiredStatus;
        }

        var response = await window.AP.requestJson("/api/v1/me/profile", {
          method: "PATCH",
          body: JSON.stringify(payload)
        });
        if (response.status === 200) {
          var body = await response.json();
          currentStatus = body.status;
          published.checked = body.status === "published";
          setStatus(status, "保存しました");
          return;
        }
        if (response.status === 401) {
          window.location.replace("/login");
          return;
        }
        var message = await window.AP.errorMessage(response);
        setStatus(error, message || "保存に失敗しました");
      } finally {
        submitButton.disabled = false;
      }
    });
  });
})();
