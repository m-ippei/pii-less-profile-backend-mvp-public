/*
 * `GET /account` のフォーム（design/05-api-contract.md §6 / design/07-security.md §8）。
 * 認証ガードを通してからパスワード変更を送る。成功時は新 `recovery_key` を持って
 * `/credentials-saved` へ進む（秘密値は URL に載せない）。
 */
(function () {
  "use strict";

  function showStatus(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  document.addEventListener("DOMContentLoaded", async function () {
    var allowed = await window.AP.requireAuth();
    if (!allowed) {
      return;
    }
    var form = document.getElementById("change-password-form");
    var error = document.getElementById("form-error");
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      showStatus(error, "");
      var response = await window.AP.requestJson("/api/v1/me/password", {
        method: "POST",
        body: JSON.stringify({
          current_password: document.getElementById("current_password").value,
          new_password: document.getElementById("new_password").value
        })
      });
      if (response.status === 200) {
        var body = await response.json();
        window.AP.stashSecrets({
          recovery_key: body.recovery_key,
          old_key_invalidated: true,
          next: "/account"
        });
        window.location.assign("/credentials-saved");
        return;
      }
      if (response.status === 401) {
        window.location.replace("/login");
        return;
      }
      var message = await window.AP.errorMessage(response);
      showStatus(error, message || "パスワード変更に失敗しました");
    });
  });
})();
