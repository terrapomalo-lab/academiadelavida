(function () {
  var f = document.getElementById("ded-filter"), t = document.getElementById("ded-table");
  if (!f || !t) return;
  f.addEventListener("input", function () {
    var q = f.value.trim().toLowerCase();
    t.querySelectorAll("tbody tr").forEach(function (tr) {
      var txt = tr.textContent.toLowerCase();
      tr.querySelectorAll("input[name=name]").forEach(function (i) { txt += " " + i.value.toLowerCase(); });
      tr.style.display = !q || txt.indexOf(q) >= 0 ? "" : "none";
    });
  });
})();
