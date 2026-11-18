/*
 * `GET /new` の作成画面（design/05-api-contract.md §6 / design/12-ux-secrets.md §13）。
 * `POST /api/v1/profiles` に空 JSON を実 CSRF 経路（csrf.js）で送り、201 の秘密値を
 * `sessionStorage` に一度だけ預けて `/credentials-saved` へ進む。秘密値は URL に載せない。
 * 二重送信で別プロフィールを2つ作らないよう、送信中はボタンを無効化する。
 */
(function () {
  "use strict";

  function setError(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.getElementById("create");
    var error = document.getElementById("form-error");

    button.addEventListener("click", async function () {
      button.disabled = true;
      setError(error, "");

      var response;
      try {
        response = await window.AP.requestJson("/api/v1/profiles", {
          method: "POST",
          body: "{}"
        });
      } catch (failure) {
        setError(error, "プロフィールの作成に失敗しました");
        button.disabled = false;
        return;
      }

      if (response.status === 201) {
        var body = await response.json();
        window.AP.stashSecrets({
          login_id: body.login_id,
          password: body.initial_password,
          recovery_key: body.recovery_key,
          next: "/profile-editor"
        });
        window.location.assign("/credentials-saved");
        return;
      }

      var message = await window.AP.errorMessage(response);
      setError(error, message || "プロフィールの作成に失敗しました");
      button.disabled = false;
    });
  });
})();
