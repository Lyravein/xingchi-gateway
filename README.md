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

## Pasang di 9Router

- Base URL: `http://127.0.0.1:8789/v1`
- Model: `gpt-6.1-sol`
- API key: kosongkan, atau isi sama dengan `GATEWAY_API_KEY` bila dipakai.

## Deploy sebagai service

Salin **folder project** (bukan hanya satu file) ke server, lalu isi `.env`.
Contoh unit systemd ada di `xingchi-gateway.service.example`. Perintah jalan tetap
`python3 gateway.py`.

## Catatan penting

- Upstream menyuntikkan prompt Codex berukuran sekitar 18k token per sesi sebagai
  `instructions`. Itu tidak bisa dihilangkan dari sisi akun, tetapi gateway selalu
  membuang `instructions` sebelum mengirim respons ke client.
- Jangan commit `.env`; file itu sudah dikecualikan lewat `.gitignore`.
