(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.getAttribute("content") : "";
  if (!token) {
    return;
  }

  document.querySelectorAll("form").forEach(function (form) {
    var method = (form.getAttribute("method") || "get").toLowerCase();
    if (method !== "post" && method !== "put" && method !== "patch" && method !== "delete") {
      return;
    }
    if (form.querySelector('input[name="csrf_token"]')) {
      return;
    }
    var input = document.createElement("input");
    input.type = "hidden";
    input.name = "csrf_token";
    input.value = token;
    form.appendChild(input);
  });

  if (typeof window.fetch === "function") {
    var originalFetch = window.fetch;
    window.fetch = function (input, init) {
      init = init || {};
      var method = String(init.method || "GET").toUpperCase();
      if (method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE") {
        var headers = new Headers(init.headers || {});
        if (!headers.has("X-CSRFToken") && !headers.has("X-CSRF-Token")) {
          headers.set("X-CSRFToken", token);
        }
        init.headers = headers;
      }
      return originalFetch.call(this, input, init);
    };
  }
})();
