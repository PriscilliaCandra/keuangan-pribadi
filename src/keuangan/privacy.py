"""Teks privasi untuk pengguna. Harus mencerminkan apa yang BENAR-BENAR dilakukan aplikasi;
ubah di sini bila perilakunya berubah dan naikkan VERSION agar persetujuan diminta ulang."""
from __future__ import annotations

VERSION = "2026-11-beta3"  # dinaikkan: target tabungan & tanggal gajian ditambahkan ke ringkasan


def summary(ai_note: str) -> str:
    return (
        "🔒 Privasi & data (versi beta)\n\n"
        "Yang disimpan di server: ID chat dan nama Telegram-mu, catatan pengeluaran dan "
        "pemasukan (jumlah, kategori, catatan, tanggal), anggaran, tagihan/langganan, target tabungan, "
        "tanggal gajian, dan "
        "hitungan pemakaian AI harian (hanya angka, tanpa isi; dibuang otomatis setelah 7 hari, "
        "termasuk bila kamu menghapus akun, untuk mencegah penyalahgunaan kuota).\n"
        "Foto struk tidak disimpan; hanya hasil bacaannya.\n"
        f"Pemrosesan AI: {ai_note}\n\n"
        "Siapa yang bisa melihat: bot ini tidak punya fitur bagi admin untuk membuka catatanmu. "
        "Namun pemilik server secara teknis memegang databasenya. Ini layanan beta, jadi jangan "
        "mencatat hal yang sangat sensitif.\n\n"
        "Kontrolmu: /ekspor (unduh semua catatan), /hapus_akun (hapus semua datamu), "
        "/privasi (pesan ini).\n"
        "Bot ini alat bantu mencatat, bukan nasihat keuangan.\n\n"
        "Dengan mengetik /setuju, kamu menyetujui hal-hal di atas."
    )
