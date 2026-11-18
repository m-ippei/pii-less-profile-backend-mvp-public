/*
 * `GET /credentials-saved` の保存確認画面（design/12-ux-secrets.md §13）。
 * 作成・パスワード変更・回復の3経路で共用する。秘密値は sessionStorage から一度だけ読み、
 * 「保存した」にチェックするまで次へ進めない。管理識別用文字は `_` 以降へ即時反映する。
 */
(function () {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function setText(id, value) {
    byId(id).textContent = value;
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return Promise.reject(new Error("clipboard unavailable"));
  }

  function buildCopyText(state) {
    var lines = [];
    if (state.login_id) {
      lines.push("ログインID: " + state.loginDisplay);
    }
    if (state.password) {
      lines.push("パスワード: " + state.passwordDisplay);
    }
    if (state.recovery_key) {
      lines.push("リカバリーキー: " + state.recoveryDisplay);
    }
    return lines.join("\n");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var payload = window.AP.takeSecrets();
    var empty = byId("empty-message");
    var content = byId("secrets-content");
    var continueButton = byId("continue");
    var checkbox = byId("saved");
    var status = byId("copy-status");
    var label = byId("identity-label");

    if (!payload) {
      empty.hidden = false;
      content.hidden = true;
      continueButton.disabled = true;
      return;
    }

    var state = {
      login_id: payload.login_id || "",
      password: payload.password || "",
      recovery_key: payload.recovery_key || "",
      label: ""
    };

    byId("secret-login-id-block").hidden = state.login_id === "";
    byId("secret-password-block").hidden = state.password === "";
    byId("secret-recovery-block").hidden = state.recovery_key === "";
    byId("old-key-note").hidden = payload.old_key_invalidated !== true;
    empty.hidden = true;
    content.hidden = false;

    function render() {
      state.label = window.AP.sanitizeLabel(label.value);
      if (label.value !== state.label) {
        label.value = state.label;
      }
      state.loginDisplay = window.AP.loginIdDisplay(state.login_id, state.label);
      state.passwordDisplay = window.AP.passwordDisplay(state.password, state.label);
      state.recoveryDisplay = window.AP.recoveryKeyDisplay(state.recovery_key, state.label);
      setText("login-id-value", state.loginDisplay);
      setText("password-value", state.password);
      setText("password-label", state.label);
      setText("recovery-key-value", state.recoveryDisplay);
    }

    function updateGate() {
      continueButton.disabled = !checkbox.checked;
    }

    label.addEventListener("input", render);
    checkbox.addEventListener("change", updateGate);
    continueButton.addEventListener("click", function () {
      window.location.assign(payload.next || "/login");
    });
    byId("copy").addEventListener("click", async function () {
      try {
        await copyText(buildCopyText(state));
        status.textContent = "コピーしました";
      } catch (error) {
        status.textContent = "コピーできませんでした";
      }
    });
    byId("save-all").addEventListener("click", async function () {
      try {
        await copyText(buildCopyText(state) + "\n");
        status.textContent = "全体をコピーしました";
      } catch (error) {
        status.textContent = "コピーできませんでした";
      }
    });

    render();
    updateGate();
  });
})();
