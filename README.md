# Keuangan Pribadi — agen AI pencatat keuangan via Telegram

Bot Telegram yang mencatat pengeluaran dan pemasukan dari chat atau foto struk, memantau anggaran,
mengingatkan tagihan, menemukan langganan terlupa dari email, dan **menjawab pertanyaan tentang
keuanganmu dalam bahasa Indonesia**. Data disimpan di SQLite lokal. AI bisa **lokal dan gratis (Ollama)**
atau Claude.

## Fitur

**Mencatat**
- Pengeluaran: `makan siang 35rb`, `bensin 50k kemarin`, `laptop 1,5jt`, `Rp 50.000 parkir`.
- Pemasukan: `gaji 8jt`, `+500rb freelance`, atau `/masuk bonus 1jt`.
- **Foto struk**: model visi membaca toko, total, tanggal, kategori; kamu konfirmasi lewat tombol ✅/❌.
- Kategori otomatis dari kata kunci (gratis, tanpa AI); kalau tidak dikenali, AI yang menebak.

**Memahami**
- `/saldo`: pemasukan, pengeluaran, selisih, dan persen ditabung bulan ini.
- Ringkasan **mingguan otomatis** tiap Minggu 19:00; `/ringkasan bulan` untuk sebulan.
- **Anggaran** per kategori dan total, dengan peringatan di 80% dan 100%.
- **`/tanya`**: tanya apa saja, mis. `berapa pengeluaran kopi bulan ini?`, `bulan apa paling boros?`,
  `berapa sisa uangku?`. Pertanyaan yang diketik langsung (diawali "berapa", "apa", "bulan apa", atau
  berakhiran `?`) otomatis dijawab agen.

**Dashboard web (hanya-baca, hanya di laptop tempat bot berjalan)**
- Kirim `/dashboard` di Telegram, lalu buka tautannya di browser laptop.
- Kartu selisih/pemasukan/pengeluaran/persen ditabung (dengan perbandingan periode sebelumnya),
  grafik kolom pemasukan vs pengeluaran (harian atau bulanan), batang kategori pengeluaran dan
  pemasukan (klik untuk memfilter transaksi), meter anggaran, daftar tagihan, dan tabel transaksi
  dengan filter. Ada tampilan tabel, tooltip, mode terang/gelap, dan layout ponsel.
- Kategori pemasukan: Gaji, Bonus & THR, Freelance & Usaha, Investasi, Lainnya (terisi otomatis dari
  kata kunci, bisa dikoreksi dengan reply atau `/edit m1 kategori bonus`).

**Merapikan**
- **Koreksi langsung di chat**: balas (reply) pesan "Dicatat…" dari bot dengan `transportasi`, `40rb`,
  `kemarin`, atau gabungan (`40rb kemarin transport`); balas `hapus` untuk menghapusnya. Tanpa reply,
  tulis eksplisit untuk catatan terakhir: `ubah jadi 40rb`, `kategorinya transport`, `harusnya 40rb`,
  `hapus yang tadi`. Kalimat seperti `ganti oli 150rb` tetap dicatat sebagai pengeluaran.
- `/riwayat` (dengan ID), `/edit 12 kategori transport`, `/edit 12 nominal 40rb`, `/hapus 12`.
- `/ekspor` mengirim file CSV (bulan ini / `semua` / `2026-10`) untuk Excel atau Google Sheets.

**Tagihan & langganan**
- Manual: `/tagihan listrik 350rb tgl 20`, `/langganan netflix 54rb tgl 5`, tahunan:
  `/langganan pajak motor 2jt tgl 10 bulan 3 tahunan`.
- **Pengingat dua tahap** (H-3 dan hari-H) tiap 08:00.
- **Baca email** (IMAP): langganan baru, masa percobaan, harga naik, atau layanan yang sudah kamu
  `/batal` tapi masih menagih. Dipindai otomatis tiap 6 jam.
- Tiap tanggal 1: tinjauan langganan yang lama tidak dikonfirmasi (≥90 hari) dan saran langganan dari
  pola pengeluaran berulang.

## Perintah

| Perintah | Fungsi |
|---|---|
| teks bebas / foto struk | catat pengeluaran |
| `gaji 8jt` · `+500rb freelance` · `/masuk …` | catat pemasukan |
| `/saldo` | arus kas bulan ini |
| `/riwayat` | 10 catatan terakhir + ID (pemasukan berawalan `m`) |
| `/edit <id> <kategori\|nominal\|tanggal\|catatan> <nilai>` | ubah catatan |
| `/hapus` · `/hapus <id>` | hapus catatan terakhir / per ID |
| `/ekspor [semua\|2026-10]` | unduh CSV |
| `/tanya <pertanyaan>` | tanya data keuanganmu |
| `/dashboard` | tautan dashboard web (hanya laptop ini) |
| `/anggaran` · `/anggaran makanan 1,5jt` · `/anggaran total 5jt` · `/anggaran hapus makanan` | anggaran |
| `/ringkasan` · `/ringkasan bulan` | ringkasan 7 hari / bulan ini |
| `/tagihan` · `/langganan` | daftar, atau tambah dengan argumen |
| `/pakai <nama>` · `/batal <nama>` | konfirmasi masih dipakai / sudah berhenti |
| `/scan_email` | pindai email sekarang |

## Mulai

Butuh Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/Mac: source .venv/bin/activate)
pip install -e .
copy .env.example .env          # lalu isi
python -m keuangan
```

1. **Token bot**: chat `@BotFather` → `/newbot` → salin token ke `TELEGRAM_BOT_TOKEN`.
2. **Chat ID**: jalankan bot dengan `ALLOWED_CHAT_IDS` kosong, kirim pesan apa saja, bot membalas Chat ID-mu. Isi ke `.env`, restart. Bot **menolak semua chat lain**.
3. **AI (opsional, pilih salah satu)**. Tanpa AI, pencatatan teks, anggaran, saldo, riwayat, edit, ekspor, dan pengingat manual tetap jalan.
   - **Ollama (gratis, lokal)**: `ollama pull qwen2.5:7b` (teks, email, `/tanya`) dan `ollama pull qwen2.5vl:7b` (foto struk), lalu isi `OLLAMA_MODEL=qwen2.5:7b` dan `OLLAMA_VISION_MODEL=qwen2.5vl:7b`. **Ollama harus sedang berjalan.**
   - **Claude (berbayar per pemakaian)**: isi `ANTHROPIC_API_KEY`. Model default `claude-opus-5-5`; untuk menekan biaya coba `ANTHROPIC_MODEL=claude-haiku-5-5` dan bandingkan hasilnya.
   - Kalau keduanya terisi, Claude dipilih; paksa dengan `LLM_PROVIDER=ollama` atau `anthropic`.
4. **Email (opsional, butuh AI)**: untuk Gmail aktifkan verifikasi 2 langkah, buat **App Password**, isi `IMAP_USER` dan `IMAP_PASSWORD`. Jangan memakai password utama.

Test: `pip install -r requirements-dev.txt && pytest`

## Privasi & keamanan

- Semua data di `data/keuangan.db` di komputermu. Jangan commit `.env` atau `data/` (sudah di `.gitignore`).
- Dengan **Ollama**, foto struk, email, dan pertanyaanmu tidak keluar dari komputer. Dengan **Claude**, itu dikirim ke API Anthropic.
- **Email**: hanya yang lolos filter kata kunci pada subjek/pengirim, dipotong 3000 karakter, dibuka read-only (tidak menandai "dibaca").
- Isi email/struk/catatan diperlakukan sebagai **data, bukan perintah**. Keluaran model divalidasi (nominal, tanggal, kategori) sebelum disimpan.
- Agen `/tanya` hanya punya **alat baca-saja** yang terkunci ke chat-mu; tidak ada alat untuk mengubah, menghapus, membayar, atau mengirim. Argumen alat dari model divalidasi dan dibatasi.
- Ekspor CSV menetralkan *formula injection* (sel yang diawali `= + - @`).
- Bot hanya merespons `ALLOWED_CHAT_IDS`.
- **Dashboard**: hanya mendengarkan `127.0.0.1`, wajib token (cookie HttpOnly + SameSite=Strict),
  memeriksa header `Host` (anti DNS-rebinding), hanya metode GET/HEAD, berkas statis dari daftar putih,
  CSP ketat tanpa skrip/gaya inline atau sumber luar, tanpa pustaka pihak ketiga (grafik SVG buatan
  sendiri), dan semua teks dari data dipasang dengan `textContent`. Data chat lain tidak bisa dibaca.
  Token acak disimpan di `data/dashboard_token`; jangan bagikan tautannya.

## Arsitektur

```
bot.py (Telegram, jadwal)  ──►  service.py (semua logika, bebas kanal)
                                   ├─ parser.py / categories.py   catat teks, pemasukan, tanggal
                                   ├─ ledger.py                   saldo, riwayat, edit, hapus, CSV
                                   ├─ budget.py / reports.py      anggaran & ringkasan
                                   ├─ bills.py                    tagihan, langganan
                                   ├─ agent.py                    alat baca-saja untuk /tanya
                                   ├─ dashboard.py + static/      dashboard web lokal (baca-saja)
                                   ├─ email_scan.py               IMAP → llm.py
                                   ├─ llm.py                      Ollama atau Claude (+ loop alat)
                                   └─ db.py                       SQLite
```

`service.py` tidak tahu soal Telegram: untuk **WhatsApp**, cukup tulis adapter baru (mis. webhook
WhatsApp Cloud API) yang memanggil `Service.handle_text` / `read_receipt` / dst.

## Batasan yang disengaja

- Mata uang rupiah saja; invoice dalam mata uang lain disimpan tanpa jumlah.
- Model lokal 7B bisa salah baca (nominal email, struk kusut). Foto struk selalu minta konfirmasi,
  dan jawaban `/tanya` sebaiknya dicek untuk keputusan penting.
- Pengingat jatuh tempo mengandalkan tanggal dari email atau input manual; tidak terhubung ke bank.
- Satu pengguna per chat; belum ada saldo awal atau banyak dompet/rekening.
- Dashboard hanya untuk melihat dan hanya terbuka di laptop yang menjalankan bot (tidak dari HP).
  Ubah `DASHBOARD_PORT` bila port 8765 dipakai program lain; `0` mematikannya.
