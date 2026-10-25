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


  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
  function renderPay(box, p, M) {
    box.appendChild(el("h3", "pay-h", p.title));
    var codes = el("div", "codes");
    var c1 = el("div", "code"); c1.appendChild(el("h4", null, M.code_hr));
    var i1 = el("img"); i1.src = p.hub3; i1.alt = "HUB3"; i1.width = 690; i1.height = 204; c1.appendChild(i1);
    var dl = el("div", "dl");
    var a1 = el("a", "btn btn-line btn-sm", M.dl_png); a1.href = p.hub3_dl; dl.appendChild(a1);
    var a2 = el("a", "btn btn-line btn-sm", M.dl_pdf); a2.href = p.pdf; dl.appendChild(a2);
    c1.appendChild(dl); codes.appendChild(c1);
    var c2 = el("div", "code qr"); c2.appendChild(el("h4", null, M.code_eu));
    var i2 = el("img"); i2.src = p.epc; i2.alt = "EPC QR"; i2.width = 300; i2.height = 300; c2.appendChild(i2);
    codes.appendChild(c2); box.appendChild(codes);
    var d = el("dl", "pay");
    p.rows.forEach(function (r) {
      var row = el("div"); row.appendChild(el("dt", null, r[0]));
      var dd = el("dd", null, r[1]); row.appendChild(dd);
      var b = el("button", "copy", M.copy); b.type = "button";
      var val = r[2] || r[1];
      b.addEventListener("click", function () { copyText(b, val, M.copied); });
      row.appendChild(b); d.appendChild(row);
    });
    box.appendChild(d);
    var pl = el("p", "note", p.page_label + " "); var lk = el("a", null, p.page); lk.href = p.page; pl.appendChild(lk); box.appendChild(pl);
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
      if (!form.querySelector("input[name=paket]:checked")) e.push("err_fee");
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
            res.j.paragraphs.forEach(function (p) { var el = document.createElement("p"); el.textContent = p; ok.appendChild(el); });
            if (res.j.pay) renderPay(ok, res.j.pay, M);
            form.hidden = true; ok.hidden = false; ok.focus();
            ok.scrollIntoView({ block: "start" });
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
    var tmr;
    input.addEventListener("input", function () {
      chips.forEach(function (x) { x.setAttribute("aria-pressed", "false"); });
      clearTimeout(tmr); tmr = setTimeout(function () { if (input.value) show(); }, 700);
    });
    document.getElementById("don-show").addEventListener("click", show);
    // kod za donaciju vidljiv odmah, s unaprijed odabranim iznosom
    var pre = don.querySelector('.chip[data-amount="20"]');
    if (pre) { pre.setAttribute("aria-pressed", "true"); input.value = "20"; }
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
    show();
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
