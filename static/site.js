(function () {
  "use strict";

  function oibValid(o) {
    if (!/^\d{11}$/.test(o)) return false;
    var a = 10;
    for (var i = 0; i < 10; i++) { a = (a + +o[i]) % 10; if (a === 0) a = 10; a = (a * 2) % 11; }
    var k = 11 - a; if (k === 10) k = 0;
    return k === +o[10];
  }
  function age(dob) {
    var d = new Date(dob + "T00:00:00"), n = new Date();
    var a = n.getFullYear() - d.getFullYear();
    if (n.getMonth() < d.getMonth() || (n.getMonth() === d.getMonth() && n.getDate() < d.getDate())) a--;
    return a;
  }
  function copyText(btn, text, okLabel) {
    var done = function () { var o = btn.textContent; btn.textContent = okLabel; setTimeout(function () { btn.textContent = o; }, 1500); };
    try {
      navigator.clipboard.writeText(text).then(done, function () {});
    } catch (e) {}
  }

  // ---------------------------------------------------------------- pristupnica
  var form = document.getElementById("apply");
  if (form) {
    var M = JSON.parse(form.getAttribute("data-msg"));
    var err = document.getElementById("apply-err");
    var ok = document.getElementById("apply-ok");
    var btn = document.getElementById("apply-btn");
    var noOib = document.getElementById("bez_oib");
    var oib = document.getElementById("oib");
    var cw = document.getElementById("country-wrap");

    noOib.addEventListener("change", function () {
      cw.hidden = !noOib.checked;
      oib.disabled = noOib.checked;
      if (noOib.checked) oib.value = "";
    });

    function check() {
      var e = [];
      var v = function (id) { return (document.getElementById(id).value || "").trim(); };
      var name = v("ime");
      if (name.length < 3 || name.indexOf(" ") < 0) e.push("err_name");
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v("email"))) e.push("err_email");
      if (noOib.checked) { if (!v("drzava")) e.push("err_country"); }
      else if (!oibValid(v("oib").replace(/\D/g, ""))) e.push("err_oib");
      var dob = v("dob");
      if (!dob) e.push("err_dob"); else if (age(dob) < 18) e.push("err_minor");
      if (!form.querySelector("input[name=vrsta]:checked")) e.push("err_type");
      if (!form.querySelector("input[name=clanarina]:checked")) e.push("err_fee");
      if (!document.getElementById("statut").checked) e.push("err_statute");
      if (!document.getElementById("punoljetnost").checked) e.push("err_adult");
      if (!document.getElementById("privatnost").checked) e.push("err_privacy");
      return e;
    }
    function showErr(text) { err.textContent = text; err.hidden = false; err.scrollIntoView({ block: "nearest" }); }

    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      var e = check();
      if (e.length) { showErr(M.err_prefix + " " + e.map(function (k) { return M[k]; }).join("; ") + "."); return; }
      err.hidden = true;
      btn.disabled = true; btn.textContent = M.sending;
      fetch(form.getAttribute("data-api"), { method: "POST", body: new FormData(form), headers: { "Accept": "application/json" } })
        .then(function (r) { return r.json().then(function (j) { return { status: r.status, j: j }; }); })
        .then(function (res) {
          if (res.status === 200 && res.j.ok) {
            ok.innerHTML = "";
            var h = document.createElement("h3"); h.textContent = res.j.title; ok.appendChild(h);
            res.j.paragraphs.forEach(function (p) { var el = document.createElement("p"); el.textContent = p.replace(/<[^>]+>/g, ""); ok.appendChild(el); });
            form.hidden = true; ok.hidden = false; ok.focus();
          } else {
            showErr(res.j.message || M.server);
          }
        })
        .catch(function () { showErr(M.server); })
        .then(function () { btn.disabled = false; btn.textContent = M.submit; });
    });
  }

  // ---------------------------------------------------------------- donacija
  var don = document.getElementById("don");
  if (don) {
    var lang = don.getAttribute("data-lang");
    var input = document.getElementById("don-amount");
    var chips = don.querySelectorAll(".chip");
    var derr = document.getElementById("don-err");
    var box = document.getElementById("don-box");
    chips.forEach(function (c) {
      c.addEventListener("click", function () {
        chips.forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
        c.setAttribute("aria-pressed", "true");
        input.value = c.getAttribute("data-amount");
        show();
      });
    });
    input.addEventListener("input", function () { chips.forEach(function (x) { x.setAttribute("aria-pressed", "false"); }); });
    document.getElementById("don-show").addEventListener("click", show);
    function fmt(v) {
      var s = v.toFixed(2);
      return lang === "hr" ? s.replace(".", ",") + " €" : "€" + s;
    }
    function show() {
      var v = parseFloat(String(input.value).replace(",", "."));
      if (!(v >= 1 && v <= 10000)) { derr.textContent = don.getAttribute("data-err"); derr.hidden = false; box.hidden = true; return; }
      v = Math.round(v * 100) / 100;
      derr.hidden = true;
      var q = "?iznos=" + v.toFixed(2);
      document.getElementById("d-amount").textContent = fmt(v);
      document.getElementById("d-amount-raw").textContent = lang === "hr" ? v.toFixed(2).replace(".", ",") : v.toFixed(2);
      document.getElementById("d-hub3").src = don.getAttribute("data-hub3") + q;
      document.getElementById("d-epc").src = don.getAttribute("data-epc") + q;
      document.getElementById("d-hub3-dl").href = don.getAttribute("data-hub3") + q + "&dl=1";
      document.getElementById("d-pdf").href = don.getAttribute("data-pdf") + q + "&lang=" + lang;
      box.hidden = false;
    }
  }

  // ---------------------------------------------------------------- kopiranje
  document.querySelectorAll(".copy").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = document.getElementById(b.getAttribute("data-copy"));
      var root = document.getElementById("don") || document.body;
      copyText(b, el.textContent.trim(), (root.getAttribute && root.getAttribute("data-copied")) || b.getAttribute("data-copied") || "✓");
    });
  });
})();
