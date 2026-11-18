/*
 * `GET /recovery` のフォーム（design/05-api-contract.md §6 / design/07-security.md §8）。
 * 回復成功時は新しい `login_id` と `recovery_key` を持って `/credentials-saved` へ進む。
 */
(function () {
  "use strict";

  function showStatus(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("recovery-form");
    var error = document.getElementById("form-error");
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      showStatus(error, "");
      var response = await window.AP.requestJson("/api/v1/recovery/password-resets", {
        method: "POST",
        body: JSON.stringify({
          recovery_key: document.getElementById("recovery_key").value,
          new_password: document.getElementById("new_password").value
        })
      });
      if (response.status === 200) {
        var body = await response.json();
        window.AP.stashSecrets({
          login_id: body.login_id,
          recovery_key: body.recovery_key,
          old_key_invalidated: true,
          next: "/login"
        });
        window.location.assign("/credentials-saved");
        return;
      }
      var message = await window.AP.errorMessage(response);
      showStatus(error, message || "回復に失敗しました");
    });
  });
})();
