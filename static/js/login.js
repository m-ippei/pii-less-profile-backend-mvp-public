/*
 * `GET /login` のフォーム（design/05-api-contract.md §6 / design/07-security.md §8）。
 * 成功 204 で `/account` へ進む。失敗は固定文言を表示する（入力値は echo しない）。
 */
(function () {
  "use strict";

  function showStatus(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.getElementById("login-form");
    var error = document.getElementById("form-error");
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      showStatus(error, "");
      var response = await window.AP.requestJson("/api/v1/sessions", {
        method: "POST",
        body: JSON.stringify({
          login_id: document.getElementById("login_id").value,
          password: document.getElementById("password").value
        })
      });
      if (response.status === 204) {
        window.location.assign("/account");
        return;
      }
      var message = await window.AP.errorMessage(response);
      showStatus(error, message || "ログインに失敗しました");
    });
  });
})();
