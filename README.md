# xingchi-gateway

Gateway lokal Python stdlib (tanpa dependency) yang membungkus upstream
`api.xingchiapi.com`. Upstream menonaktifkan `/v1/chat/completions` dan menolak
client non-pemrograman, jadi gateway ini memakai `POST /v1/responses` dengan
User-Agent client pemrograman, lalu menerjemahkan balik ke format Chat Completions.

## Jalan cepat

```bash
cp .env.example .env
# isi XINGCHI_API_KEY sendiri di .env
python3 gateway.py
```

Default: `127.0.0.1:8789`, model `gpt-6.1-sol`.

## Contoh curl

Non-stream:

```bash
curl -s http://127.0.0.1:8789/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","messages":[{"role":"user","content":"Halo"}]}'
```

Stream:

```bash
curl -N http://127.0.0.1:8789/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-6.1-sol","stream":true,"messages":[{"role":"user","content":"Halo"}]}'
```

Bila `GATEWAY_API_KEY` diisi, tambahkan header `Authorization: Bearer <key>`.

## Multi-key dan grup

Atur file key JSON lewat `GATEWAY_KEYS_FILE` (default `keys.json`) dan kelola
grup/key tanpa restart melalui GUI admin. Aktifkan GUI dengan
`GATEWAY_ADMIN_TOKEN=<token-panjang>`, lalu buka `http://127.0.0.1:8789/admin`.

Rute lama `/v1/...` tetap memakai key env `XINGCHI_API_KEY`. Untuk grup bernama
misalnya `gpt`, gunakan rute:

- `/gpt/v1/models`
- `/gpt/v1/chat/completions`
- `/gpt/v1/responses`

Format file `GATEWAY_KEYS_FILE` berisi grup, slug, protokol, dan daftar key.
Secret key hanya disimpan dengan permission `0600`; GUI hanya menampilkan 4
karakter terakhir. Jaga `GATEWAY_ADMIN_TOKEN` dan jangan commit file key JSON.

## Pasang di 9Router

- Base URL: `http://127.0.0.1:8789/v1`
- Model: `gpt-6.1-sol`
- API key: kosongkan, atau isi sama dengan `GATEWAY_API_KEY` bila dipakai.

## Deploy sebagai service

Salin **folder project** (bukan hanya satu file) ke server, lalu isi `.env`.
Contoh unit systemd ada di `xingchi-gateway.service.example`. Perintah jalan tetap
`python3 gateway.py`.

## Debug dump

Untuk investigasi isi request/response:

```bash
GATEWAY_DEBUG_DUMP=1 python3 gateway.py
```

- `GATEWAY_DEBUG_DUMP=1` (juga `true`/`yes`) menyalakan dump; default mati.
- `GATEWAY_DEBUG_DIR` menentukan folder tujuan; default `debug/` di direktori
  yang sama dengan file key.
- `GATEWAY_DEBUG_MAX_BODY` membatasi ukuran body per file (default 262144 byte);
  body yang lebih besar dipotong dan ditandai `truncated`.

Setiap request chat/responses menghasilkan hingga tiga file: `<id>-inbound.json`
(body client), `<id>-upstream.json` (body yang dikirim ke upstream), dan
`<id>-response.json` (body/SSE respons). Isinya adalah prompt dan respons asli:
simpan lokal, jangan dibagikan, dan matikan lagi setelah investigasi selesai.
Secret key tidak pernah ikut; hanya label dan 4 digit terakhir.

## Statistik usage

Satu baris JSONL per request chat/responses dicatat ke `stats.jsonl` (default di
direktori yang sama dengan file key). Ubah lokasinya dengan `GATEWAY_STATS_FILE`,
atau matikan dengan `GATEWAY_STATS_FILE=off`. File dirotasi ke `stats.jsonl.1`
saat melewati 5 MB.

Ringkasan tersedia di `GET /admin/api/stats` (Bearer admin token) dan pada bagian
Usage di halaman admin: per grup hari ini, 7 hari terakhir, serta 10 request
terakhir.

## Catatan penting

- Upstream menyuntikkan prompt Codex berukuran sekitar 18k token per sesi sebagai
  `instructions`. Itu tidak bisa dihilangkan dari sisi akun, tetapi gateway selalu
  membuang `instructions` sebelum mengirim respons ke client.
- Jangan commit `.env`; file itu sudah dikecualikan lewat `.gitignore`.

## Lisensi

MIT, lihat berkas [LICENSE](LICENSE).
