"use strict";
// Tautan dari Telegram: /?t=TOKEN. Token baru ditukar saat tombol ditekan (POST),
// bukan saat halaman dibuka, supaya pengambil pratinjau tautan tidak menghabiskannya.
(() => {
  const msg = document.getElementById("msg");
  const btn = document.getElementById("enter");
  const token = new URLSearchParams(location.search).get("t");

  if (!token) {
    msg.textContent = "Kamu belum masuk.";
    return;
  }
  history.replaceState(null, "", "/");  // buang token dari bilah alamat & riwayat
  msg.textContent = "Tautan siap dipakai.";
  btn.hidden = false;
  btn.focus();
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    msg.textContent = "Masuk…";
    try {
      const res = await fetch("/api/login", {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token }),
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) { location.replace("/"); return; }
      msg.textContent = body.error || `Galat ${res.status}`;
    } catch (_) {
      msg.textContent = "Tidak bisa menghubungi server.";
    }
    btn.hidden = true;  // token sekali pakai: minta tautan baru
  });
})();
