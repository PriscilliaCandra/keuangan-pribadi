# Panduan deploy beta tertutup

Untuk menjalankan bot sebagai layanan di server (VPS atau PC yang selalu menyala) bagi beberapa
pengguna dengan kode undangan. Ini bukan panduan lengkap produksi; baca bagian "Sebelum menarik bayaran".

## 1. Server
- Linux (Ubuntu 22.04+), Python 3.11+, akses SSH. 2 vCPU / 4 GB cukup untuk bot + dashboard tanpa AI lokal.
- **AI lokal (Ollama) butuh lebih besar**: model 7B di CPU lambat (puluhan detik per struk) dan memakai
  RAM 6-8 GB; GPU 8 GB+ jauh lebih nyaman. Ollama memproses satu permintaan per waktu, jadi antrean
  (`AI_MAX_CONCURRENT=1`) dan kuota harian (`AI_DAILY_LIMIT`) penting. Perkirakan: 10-20 pengguna
  dengan kuota 30/hari bisa menumpuk antrean di jam sibuk. Pantau, dan bila perlu pindah ke API cloud.
- Server Ollama boleh di mesin lain: isi `OLLAMA_HOST=http://alamat:11434` (jangan buka porta 11434 ke internet).

## 2. Pasang
```bash
git clone https://github.com/PriscilliaCandra/keuangan-pribadi.git && cd keuangan-pribadi
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
cp .env.example .env && chmod 600 .env     # isi TELEGRAM_BOT_TOKEN, ADMIN_CHAT_IDS, pengaturan AI
```
Buat pengguna sistem terpisah (mis. `keuangan`) yang hanya boleh menulis di folder `data/`.

## 3. Jalan sebagai layanan (systemd)
`/etc/systemd/system/keuangan.service`:
```ini
[Unit]
Description=Bot keuangan pribadi
After=network-online.target

[Service]
User=keuangan
WorkingDirectory=/opt/keuangan-pribadi
ExecStart=/opt/keuangan-pribadi/.venv/bin/python -m keuangan
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/keuangan-pribadi/data

[Install]
WantedBy=multi-user.target
```
`sudo systemctl enable --now keuangan` · log: `journalctl -u keuangan -f` dan `data/bot.log`.

## 4. Dashboard publik (HTTPS wajib)
Server dashboard sengaja hanya mendengarkan `127.0.0.1`. Pasang proxy TLS di depannya, mis. **Caddy**:
```
uang.contoh.id {
    reverse_proxy 127.0.0.1:8765
}
```
Lalu di `.env`:
```
DASHBOARD_PUBLIC_URL=https://uang.contoh.id
DASHBOARD_TRUST_PROXY=true        # hanya karena proxy itu milikmu sendiri
```
Cookie sesi otomatis berflag `Secure` (+ HSTS) bila `DASHBOARD_PUBLIC_URL` berawalan `https://`.
Jangan membuka porta 8765 langsung ke internet.

## 5. Cadangan
- Bot membuat cadangan konsisten `data/backups/keuangan-*.db` tiap 03:30 (simpan 14 terbaru).
- **Salin keluar mesin** (rsync/rclone ke penyimpanan lain), idealnya **terenkripsi** (mis. `age`/`gpg`),
  karena berisi data keuangan pengguna. Uji pemulihan sekali: salin berkas ke `data/keuangan.db` saat bot mati.
- Aktifkan enkripsi disk di server.

## 6. Mengundang pengguna
1. Admin kirim `/undang` (atau `/undang 5 untuk grup A` untuk 5x pakai) ke bot → dapat kode + tautan.
2. Calon pengguna membuka tautan / mengetik `/daftar KODE`, membaca ringkasan privasi, lalu `/setuju`.
3. `/pengguna` (hanya metadata), `/blokir <id>`, `/aktifkan <id>`, `/statistik`.
4. Pengguna bisa `/ekspor` datanya dan `/hapus_akun` kapan saja.

## Sebelum menarik bayaran (jangan dilewati)
Ini data keuangan pribadi orang lain. Beta tertutup gratis dengan orang yang kamu kenal itu wajar; menarik bayaran dari
publik membawa kewajiban tambahan. Hal-hal yang perlu ditinjau **ahli hukum** (saya bukan penasihat hukum):
- Kebijakan privasi & syarat layanan tertulis, dasar pemrosesan data, dan retensi data.
- Kewajiban menurut UU Pelindungan Data Pribadi (UU 27/2022): persetujuan, hak akses/hapus, pemberitahuan insiden.
- Apakah ada kewajiban pendaftaran/izin tertentu untuk layanan yang kamu tawarkan.
- Teks `/privasi` di `src/keuangan/privacy.py` hanyalah ringkasan jujur perilaku aplikasi, **bukan** dokumen hukum.

## Yang belum ada (batasan beta)
- Pengguna di zona waktu lain tetap menerima pengingat 08:00 WIB.
- Tidak ada pembayaran otomatis, paket, atau batas fitur per paket.
- Fitur baca email hanya untuk admin (satu kotak surat). Mengelola kredensial email banyak orang tidak ditawarkan.
- Satu proses bot; belum ada pemantauan/alarm otomatis (pantau log dan `/statistik`).
