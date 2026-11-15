// BOX NOW karta paketomata: odabrani paketomat upisuje se u polje obrasca.
window._bn_map_widget_config = window.bn_map_widget_config = {
  parentElement: "#boxnowmap",
  type: "popup",
  afterSelect: function (s) {
    var f = document.getElementById("b-box");
    if (!f || !s) return;
    var parts = [s.boxnowLockerAddressLine1, s.boxnowLockerPostalCode].filter(Boolean).join(", ");
    f.value = (s.boxnowLockerId ? "[" + s.boxnowLockerId + "] " : "") + parts;
    f.dispatchEvent(new Event("change"));
  }
};
