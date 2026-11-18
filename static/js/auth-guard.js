/*
 * 認証ガード（design/05-api-contract.md §6 Web画面 / design/07-security.md §8）。
 * 唯一の認証付き GET `GET /api/v1/me/profile` を probe にし、401 で `/login` へ遷移する。
 */
(function () {
  "use strict";

  async function requireAuth() {
    var response = await window.AP.requestJson("/api/v1/me/profile");
    if (response.status === 401) {
      window.location.replace("/login");
      return null;
    }
    return response;
  }

  window.AP = window.AP || {};
  window.AP.requireAuth = requireAuth;
})();
