# Keuangan Pribadi — agen AI pencatat keuangan via Telegram

Bot Telegram yang mencatat pengeluaran dan pemasukan dari chat atau foto struk, memantau anggaran,
mengingatkan tagihan, menemukan langganan terlupa dari email, dan **menjawab pertanyaan tentang
keuanganmu dalam bahasa Indonesia**. Data disimpan di SQLite. AI bisa **lokal dan gratis (Ollama)**
atau Claude.

**Multi-pengguna**: setiap orang punya akunnya sendiri (identitasnya diverifikasi Telegram; tanpa
kata sandi). Pendaftaran bisa **`REGISTRATION=invite`** (hanya dengan kode undangan) atau
**`REGISTRATION=open`** (siapa pun yang menemukan bot bisa `/daftar` sendiri, dengan batas total
pengguna dan batas laju pendaftaran). Tiap pengguna hanya melihat datanya sendiri, menyetujui ringkasan privasi saat mendaftar, punya kuota AI
harian, dan bisa `/ekspor` atau `/hapus_akun` kapan saja. Admin membuat undangan dan mengelola pengguna
tetapi **tidak punya perintah untuk membaca catatan pengguna**. Lihat [docs/DEPLOY.md](docs/DEPLOY.md).

## Fitur

**Mencatat**
- Pengeluaran: `makan siang 35rb`, `bensin 50k kemarin`, `laptop 1,5jt`, `Rp 50.000 parkir`.
- **Bahasa sehari-hari**: `goceng` (5rb), `ceban` (10rb), `noban` (20rb), `gocap` (50rb), `cepek` (100rb),
  `sejuta`; bisa dikalikan: `bakso 2 goceng` = 10rb.
- Pemasukan: `gaji 8jt`, `+500rb freelance`, atau `/masuk bonus 1jt`.
- **Foto struk**: model visi membaca toko, total, tanggal, kategori; kamu konfirmasi lewat tombol ✅/❌.
- Kategori otomatis dari kata kunci (gratis, tanpa AI); kalau tidak dikenali, AI yang menebak.

**Memahami**
- `/saldo`: pemasukan, pengeluaran, selisih, dan persen ditabung bulan ini.
- Ringkasan **mingguan otomatis** tiap Minggu 19:00; `/ringkasan bulan` untuk sebulan.
- **Anggaran** per kategori dan total, dengan peringatan di 80% dan 100% dan **prediksi**: "dengan laju ini
  anggaran makanan habis sekitar 23 Nov".
- **Uang aman per hari sampai gajian** (`/aman`): atur tanggal gajian dengan `/gajian 25`. Dihitung dari
  pemasukan siklus ini dikurangi pengeluaran, tagihan yang jatuh tempo sebelum gajian, dan setoran tabungan
  (atau dari sisa anggaran total bila diatur), plus perbandingan dengan rata-rata 7 harimu dan perkiraan kapan
  uang habis.
- **Target tabungan**: `/target liburan bali 5jt sampai 2027-03`, lalu `nabung 500rb liburan` (atau
  `/tabung`, `/tarik`). Ada progres dan saran setoran per bulan. "tabung gas 150rb" tetap dicatat sebagai
  pengeluaran.
- **`/tanya`**: tanya apa saja, mis. `berapa pengeluaran kopi bulan ini?`, `bulan apa paling boros?`,
  `berapa sisa uangku?`. Pertanyaan yang diketik langsung (diawali "berapa", "apa", "bulan apa", atau
  berakhiran `?`) otomatis dijawab agen.

**Utang-piutang & patungan**
- Catat dengan kalimat biasa: `Budi utang 50rb makan siang`, `utang ke Siti 30rb buat bensin`,
  `pinjamin Budi 100rb`, `pinjam 50rb dari Budi`, opsional `tempo 2026-12-20` / `tempo besok`.
- Pelunasan: `Budi bayar 20rb` (cicilan boleh), `Budi udah bayar`, `bayar utang ke Siti 10rb`, `/lunas Budi`.
  Bot hanya menganggap itu pelunasan bila orangnya memang punya utang terbuka (kalimat lain tetap pengeluaran).
- `/utang` menampilkan semua (siapa berutang padamu, kamu berutang ke siapa), `/tagih Budi` membuat **draf pesan
  penagihan yang sopan** untuk kamu salin dan kirim sendiri (bot tidak pernah mengirim pesan ke orang lain).
- Pengingat jatuh tempo tiga tahap (H-3, hari-H, sekali setelah lewat).
- **Patungan**: `patungan makan malam 240rb dengan Budi, Siti, Ani` (dibagi rata), `... dibayar Budi`
  (temanmu yang membayar, jadi kamu berutang), `patungan hadiah 300rb bagi Budi 120rb, Siti 80rb` (bagian
  berbeda), atau `patungan makan 200rb 4 orang` (hanya bagianmu). Selalu ada **pratinjau dan konfirmasi
  "ya"/"batal"** sebelum disimpan. Hanya **bagianmu** menjadi pengeluaran; sisanya menjadi piutang.
  Sisa pembulatan ditanggung pembayar, jadi jumlahnya selalu pas.
- Utang-piutang dicatat **terpisah** dari pengeluaran/pemasukan (meminjamkan uang bukan belanja).

**Dashboard web (hanya-baca, login per pengguna)**
- Kirim `/dashboard` di Telegram → tautan masuk **sekali pakai (10 menit)**; tekan "Masuk" di halamannya.
  Sesi berlaku 7 hari dan bisa diakhiri dengan tombol Keluar.
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
- `/ekspor` mengirim CSV transaksi (bulan ini / `2026-10`); `/ekspor semua` mengirim **ZIP berisi seluruh datamu**
  (transaksi, anggaran, tagihan, target + setoran, utang + pembayaran, catatan rutin, pengaturan).

**Catatan rutin & pengingat malam**
- `kos 1,5jt tiap bulan tgl 1` atau `/rutin gaji 8jt tgl 25 masuk` → dicatat otomatis tiap bulan
  (tanggal 31 jatuh di hari terakhir bulan pendek). `/rutin` melihat daftar; `/rutin jeda|lanjut|hapus <nama>`.
- Bila tanggalnya bulan ini sudah lewat, bulan ini tidak dicatat otomatis (mencegah ganda); mulai bulan depan.
- Bot dinyalakan terlambat/laptop mati pukul 08:00? Pekerjaan harian dijalankan lagi ±90 detik setelah bot
  menyala dan tak pernah mencatat dua kali.
- `/ingatkan on` → pukul 20:30 diingatkan bila hari itu belum ada catatan (hanya bila kamu aktif mencatat
  dalam 14 hari terakhir). Bawaan: mati.

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
| `/gajian 25` · `/aman` | atur tanggal gajian · uang aman per hari sampai gajian |
| `/utang` · `/utang Siti 30rb` · `/piutang Budi 50rb makan siang` | daftar · aku berutang · orang berutang padaku |
| `/lunas Budi [20rb]` · `/tagih Budi` · `/patungan makan 240rb dengan Budi, Siti` | pelunasan · draf tagihan · patungan |
| `/target <nama> <jumlah> [sampai 2027-03]` · `/tabung <nama> <jumlah>` · `/tarik …` | target tabungan |
| `/riwayat` | 10 catatan terakhir + ID (pemasukan berawalan `m`) |
| `/edit <id> <kategori\|nominal\|tanggal\|catatan> <nilai>` | ubah catatan |
| `/hapus` · `/hapus <id>` | hapus catatan terakhir / per ID |
| `/ekspor [2026-10]` · `/ekspor semua` | unduh CSV bulan · ZIP seluruh data |
| `/rutin [jeda\|lanjut\|hapus <nama>]` · `/ingatkan on\|off` | catatan otomatis bulanan · pengingat malam |
| `/tanya <pertanyaan>` | tanya data keuanganmu |
| `/dashboard` | tautan masuk dashboard web (sekali pakai) |
| `/anggaran` · `/anggaran makanan 1,5jt` · `/anggaran total 5jt` · `/anggaran hapus makanan` | anggaran |
| `/ringkasan` · `/ringkasan bulan` | ringkasan 7 hari / bulan ini |
| `/tagihan` · `/langganan` | daftar, atau tambah dengan argumen |
| `/pakai <nama>` · `/batal <nama>` | konfirmasi masih dipakai / sudah berhenti |
| `/scan_email` | pindai email sekarang (admin) |
| `/daftar [KODE]` · `/setuju` · `/privasi` | buat akun (kode hanya di mode undangan), persetujuan, ringkasan privasi |
| `/hapus_akun` | hapus semua datamu (butuh konfirmasi `YA-HAPUS`) |
| `/undang [n] [catatan]` · `/pengguna` · `/blokir <id>` · `/aktifkan <id>` · `/statistik` | khusus admin |

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
2. **Admin**: jalankan bot dengan `ADMIN_CHAT_IDS` kosong, kirim pesan apa saja, bot membalas Chat ID-mu. Isi ke `.env`, restart. Pengguna lain masuk lewat **kode undangan** dari `/undang`, atau mendaftar sendiri bila `REGISTRATION=open` (hanya chat pribadi; grup diabaikan).
3. **AI (opsional, pilih salah satu)**. Tanpa AI, pencatatan teks, anggaran, saldo, riwayat, edit, ekspor, dan pengingat manual tetap jalan.
   - **Ollama (gratis, lokal)**: `ollama pull qwen2.5:7b` (teks, email, `/tanya`) dan `ollama pull qwen2.5vl:7b` (foto struk), lalu isi `OLLAMA_MODEL=qwen2.5:7b` dan `OLLAMA_VISION_MODEL=qwen2.5vl:7b`. **Ollama harus sedang berjalan.**
   - **Claude (berbayar per pemakaian)**: isi `ANTHROPIC_API_KEY`. Model default `claude-opus-5-5`; untuk menekan biaya coba `ANTHROPIC_MODEL=claude-haiku-5-5` dan bandingkan hasilnya.
   - Kalau keduanya terisi, Claude dipilih; paksa dengan `LLM_PROVIDER=ollama` atau `anthropic`.
4. **Email (opsional, butuh AI, khusus admin)**: untuk Gmail aktifkan verifikasi 2 langkah, buat **App Password**, isi `IMAP_USER` dan `IMAP_PASSWORD`. Jangan memakai password utama. Fitur ini sengaja tidak ditawarkan ke pengguna lain (menyimpan kredensial email banyak orang terlalu berisiko).

Test: `pip install -r requirements-dev.txt && pytest`

## Privasi & keamanan

- Semua data di `data/keuangan.db` di server yang menjalankan bot (pemiliknya secara teknis memegang database ini). Jangan commit `.env` atau `data/` (sudah di `.gitignore`). Cadangan harian otomatis di `data/backups/`.
- Dengan **Ollama**, foto struk dan pertanyaan tidak keluar dari server. Dengan **Claude**, itu dikirim ke API Anthropic. Foto struk tidak disimpan, hanya hasil bacaannya.
- **Email**: hanya yang lolos filter kata kunci pada subjek/pengirim, dipotong 3000 karakter, dibuka read-only (tidak menandai "dibaca").
- Isi email/struk/catatan diperlakukan sebagai **data, bukan perintah**. Keluaran model divalidasi (nominal, tanggal, kategori) sebelum disimpan.
- Agen `/tanya` hanya punya **alat baca-saja** yang terkunci ke chat-mu; tidak ada alat untuk mengubah, menghapus, membayar, atau mengirim. Argumen alat dari model divalidasi dan dibatasi.
- Ekspor CSV menetralkan *formula injection* (sel yang diawali `= + - @`).
- **Akses**: hanya pengguna aktif (kode undangan + persetujuan) dan admin yang dilayani; chat grup diabaikan.
  Mode undangan: kode 8 karakter acak, kedaluwarsa 7 hari, pemakaian atomik, tebakan dibatasi 5x/15 menit.
  Mode terbuka: batas total pengguna (`MAX_USERS`) dan batas pendaftaran baru per jam (`SIGNUPS_PER_HOUR`);
  akun baru tetap harus menyetujui privasi sebelum bisa memakai apa pun.
  Tiap kueri data berkunci `chat_id`, dan ada tes yang memastikan pengguna A tidak bisa membaca, mengubah,
  atau menghapus data B (lewat perintah, balasan/reply, ekspor, `/tanya`, maupun dashboard).
- **Kuota AI**: batas harian per pengguna dan antrean global, supaya satu orang tidak menghabiskan server.
  Hitungan pemakaian (angka saja) disimpan 7 hari, termasuk setelah hapus akun, supaya kuota tak bisa
  direset dengan hapus-akun lalu daftar ulang.
- **Dashboard**: hanya mendengarkan `127.0.0.1` (untuk akses publik pasang proxy TLS, lihat docs/DEPLOY.md).
  Login lewat token sekali pakai (disimpan sebagai hash) yang ditukar dengan tombol, bukan saat tautan dibuka,
  supaya pengambil pratinjau tautan tak menghabiskannya; sesi acak di cookie HttpOnly + SameSite=Strict
  (+ Secure di HTTPS); data selalu milik pemilik sesi (tidak bisa ditentukan lewat parameter); pengguna
  yang diblokir kehilangan sesinya; pembatas percobaan login per IP; header `Host` diperiksa; satu-satunya
  metode tulis adalah login/logout; CSP ketat tanpa skrip/gaya inline atau sumber luar; grafik SVG buatan
  sendiri tanpa pustaka pihak ketiga; semua teks dari data dipasang dengan `textContent`.

## Arsitektur

```
bot.py (Telegram, jadwal)  ──►  service.py (semua logika, bebas kanal)
                                   ├─ parser.py / categories.py   catat teks, pemasukan, tanggal
                                   ├─ ledger.py                   saldo, riwayat, edit, hapus, CSV
                                   ├─ budget.py / reports.py      anggaran & ringkasan
                                   ├─ bills.py                    tagihan, langganan
                                   ├─ agent.py                    alat baca-saja untuk /tanya
                                   ├─ access.py / accounts.py     undangan, persetujuan, admin, kuota AI
                                   ├─ privacy.py / security.py    teks privasi, pembatas percobaan
                                   ├─ dashboard.py                kalkulasi data dashboard
                                   ├─ dashboard_server.py + static/  server web baca-saja + login & sesi
                                   ├─ backup.py                   cadangan harian database
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
- Belum ada saldo awal atau banyak dompet/rekening.
- Dashboard hanya untuk melihat. Akses dari luar server butuh proxy HTTPS (docs/DEPLOY.md); tanpa itu hanya
  terbuka di mesin yang menjalankan bot. `DASHBOARD_PORT=0` mematikannya.
- Beta tertutup: belum ada pembayaran/paket, pengingat memakai zona waktu server (WIB), dan baca email hanya admin.
- **Bukan dokumen hukum**: sebelum menarik bayaran dari publik, tinjau kebijakan privasi/syarat layanan dan
  kepatuhan UU Pelindungan Data Pribadi bersama ahli hukum.
