/*
 * 秘密値のクライアント側保持と表示用フォーマット（design/12-ux-secrets.md §13）。
 * URL には載せず、sessionStorage に一度だけ預けて表示後に消す。サーバーへは送らない。
 */
(function () {
  "use strict";

  var STORAGE_KEY = "ap_secrets";
  var MAX_LABEL = 64;

  function stash(payload) {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  }

  function take() {
    var raw = window.sessionStorage.getItem(STORAGE_KEY);
    window.sessionStorage.removeItem(STORAGE_KEY);
    if (!raw) {
      return null;
    }
    try {
      return JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  function sanitizeLabel(value) {
    return String(value || "")
      .replace(/[\u0000-\u001f\u007f]/g, "")
      .slice(0, MAX_LABEL);
  }

  function loginIdDisplay(base, label) {
    return label ? base + "_" + label : base;
  }

  function recoveryKeyDisplay(base, label) {
    var marked = base + "_RECOVERY-KEY";
    return label ? marked + "-" + label : marked;
  }

  function passwordDisplay(base, label) {
    return label ? base + "\uFF08" + label + "\uFF09" : base;
  }

  window.AP = window.AP || {};
  window.AP.MAX_LABEL = MAX_LABEL;
  window.AP.stashSecrets = stash;
  window.AP.takeSecrets = take;
  window.AP.sanitizeLabel = sanitizeLabel;
  window.AP.loginIdDisplay = loginIdDisplay;
  window.AP.recoveryKeyDisplay = recoveryKeyDisplay;
  window.AP.passwordDisplay = passwordDisplay;
})();
