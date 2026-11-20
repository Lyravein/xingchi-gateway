# Arsitektur xingchi-gateway

Gateway lokal berbahasa Python stdlib yang menerjemahkan klien Chat Completions
ke `POST /v1/responses` milik upstream xingchi, plus passthrough Responses.

## Peta modul

| Modul | Tugas | Batas |
| --- | --- | --- |
| `gateway.py` | Entry point: impor `run` lalu jalankan server. | Tidak berisi logika protokol/konfigurasi. |
| `xingchi_gateway/config.py` | Dataclass `GatewayConfig`, pemuatan `.env`, default env, serta validasi port/timeout dan env `keys_file`/`admin_token`/`debug_*`/`stats_file`. | Tidak membuka jaringan, tidak menjalankan server. |
| `xingchi_gateway/translate.py` | Fungsi murni terjemahan Chat -> Responses, Responses -> Chat, usage, finish_reason, tools, dan pemetaan event SSE per event (`ChatStreamState`). | Tanpa I/O, tanpa env, tanpa soket. |
| `xingchi_gateway/upstream.py` | Satu-satunya modul jaringan upstream: header `Authorization`/UA, `urlopen` dengan key eksplisit per request, pembacaan SSE, dan konversi kegagalan menjadi `UpstreamFailure`. | Tidak menulis respons ke client. |
| `xingchi_gateway/keystore.py` | Penyimpanan JSON atomik 0600, hot reload, lock thread-safe, round-robin key per grup, dan data tampilan masked. | Tidak membuka jaringan. |
| `xingchi_gateway/admin.py` | API grup/key dan `GET /admin/api/stats` dengan auth Bearer admin, validasi upstream, deteksi protokol, serta render data masked; memuat `admin.html` sekali saat impor. | Tidak menulis secret ke log. |
| `xingchi_gateway/admin.html` | Aset satu halaman admin (HTML/CSS/JS inline) yang dibaca `admin.py` dari disk, termasuk bagian Usage; wajib ikut tersalin saat deploy. | Tanpa aset eksternal/framework; hanya bicara ke `/admin/api/*`. |
| `xingchi_gateway/handler_io.py` | Mixin `HandlerIO`: tulis body/JSON/SSE ke client, kirim error standar, baca dan parse request body. | Tidak memilih rute, tidak memanggil upstream, tidak memeriksa auth. |
| `xingchi_gateway/proxy.py` | Mixin `ProxyMixin`: proxy models/responses/chat, passthrough dan terjemahan SSE, konversi kegagalan upstream ke respons, serta kait dump debug dan statistik usage per request. | Tidak memilih rute atau memeriksa auth gateway. |
| `xingchi_gateway/debugdump.py` | Dump JSON 0600 per request (`inbound`/`upstream`/`response`) saat `GATEWAY_DEBUG_DUMP` nyala, plus clamp teks dan akumulator SSE. | Tidak menyentuh jaringan, tidak mencatat secret, kegagalan hanya ke stderr. |
| `xingchi_gateway/stats.py` | Satu baris JSONL per request model selesai (rotasi 5 MB) dan agregat `today`/`last_7_days`/`recent` untuk admin. | Tidak menyentuh jaringan, tidak mencatat secret, kegagalan hanya ke stderr. |
| `xingchi_gateway/server.py` | `ThreadingHTTPServer`, `GatewayHandler` (gabungan `HandlerIO` + `ProxyMixin` + `BaseHTTPRequestHandler`), auth gateway, routing, logging, dan helper `close_quietly`/`sanitize_json_body`. | Delegasi I/O ke `handler_io`, jaringan ke `upstream`, key grup ke `keystore`, dan GUI/API ke `admin`. |
| `tests/` | `helpers.py` (upstream palsu + base test case) dan enam file tes per area: `test_translate_proxy.py` (1..10), `test_keystore.py` (11..13), `test_group_routing.py` (14, 15, 19), `test_admin.py` (16..18, 20..25), `test_debugdump.py` (26..30), `test_stats.py` (31..35). | Hanya untuk repo; tidak ikut dideploy. |

## Alur

1. Client memanggil gateway di `127.0.0.1:8789`.
2. `server.GatewayHandler` memvalidasi auth gateway lalu memilih endpoint.
3. Untuk `/v1/chat/completions`, `translate.translate_chat_request` mengubah body
   ke format Responses; `proxy.ProxyMixin` mengirim via `upstream.request_upstream`.
4. Respons upstream diterjemahkan kembali oleh `translate` (non-stream) atau
   dipetakan per event SSE oleh `ChatStreamState` (stream).
5. `instructions` dari upstream selalu dibuang (`strip_instructions`) agar prompt
   injeksi akun tidak bocor ke client.
