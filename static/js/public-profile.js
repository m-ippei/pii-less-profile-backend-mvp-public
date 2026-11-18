/*
 * 公開ページ `GET /profiles/{public_id}` の描画
 * （design/05-api-contract.md §6 Web画面 / design/07-security.md §8）。
 *
 * `public_id` は URL のパスからだけ取り出す（query・fragment は使わない）。認証情報は
 * 載せない。`GET /api/v1/profiles/{public_id}` を呼び、200 は本文を `textContent` で
 * 描画する。クロスサイトスクリプティング対策として、DOM へ生 HTML を挿入する禁止 API は
 * 使わない。404（不存在・非公開・形式違反）は固定文言だけを表示し、入力値を echo しない。
 */
(function () {
  "use strict";

  var NOT_FOUND_MESSAGE = "プロフィールが見つかりません";
  var PATH_PREFIX = "/profiles/";

  function setMessage(element, message) {
    element.textContent = message;
    element.hidden = message === "";
  }

  function publicIdFromPath(pathname) {
    if (pathname.indexOf(PATH_PREFIX) !== 0) {
      return "";
    }
    var rest = pathname.slice(PATH_PREFIX.length);
    var slash = rest.indexOf("/");
    return slash === -1 ? rest : rest.slice(0, slash);
  }

  document.addEventListener("DOMContentLoaded", async function () {
    var article = document.getElementById("profile");
    var error = document.getElementById("profile-error");
    var publicId = publicIdFromPath(window.location.pathname);

    if (publicId === "") {
      setMessage(error, NOT_FOUND_MESSAGE);
      return;
    }

    var response = await window.AP.requestJson("/api/v1/profiles/" + publicId);
    if (response.status === 200) {
      var body = await response.json();
      article.textContent = body.content || "";
      return;
    }
    setMessage(error, NOT_FOUND_MESSAGE);
  });
})();
