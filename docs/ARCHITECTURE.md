# Arsitektur xingchi-gateway

Gateway lokal berbahasa Python stdlib yang menerjemahkan klien Chat Completions
ke `POST /v1/responses` milik upstream xingchi, plus passthrough Responses.

## Peta modul

| Modul | Tugas | Batas |
| --- | --- | --- |
| `gateway.py` | Entry point: impor `run` lalu jalankan server. | Tidak berisi logika protokol/konfigurasi. |
| `xingchi_gateway/config.py` | Dataclass `GatewayConfig`, pemuatan `.env`, default env, dan validasi (port, timeout, API key wajib). | Tidak membuka jaringan, tidak menjalankan server. |
| `xingchi_gateway/translate.py` | Fungsi murni terjemahan Chat -> Responses, Responses -> Chat, usage, finish_reason, tools, dan pemetaan event SSE per event (`ChatStreamState`). | Tanpa I/O, tanpa env, tanpa soket. |
| `xingchi_gateway/upstream.py` | Satu-satunya modul jaringan upstream: header `Authorization`/UA, `urlopen`, pembacaan SSE, dan konversi `HTTPError`/gangguan jaringan menjadi `UpstreamFailure`. | Tidak menulis respons ke client. |
| `xingchi_gateway/server.py` | `ThreadingHTTPServer`, routing endpoint, auth `GATEWAY_API_KEY`, logging stderr, penulisan JSON/SSE, penanganan client putus. | Tidak membangun request upstream sendiri (delegasi ke `upstream`). |

## Alur

1. Client memanggil gateway di `127.0.0.1:8789`.
2. `server.GatewayHandler` memvalidasi auth gateway lalu memilih endpoint.
3. Untuk `/v1/chat/completions`, `translate.translate_chat_request` mengubah body
   ke format Responses; `server` mengirim via `upstream.request_upstream`.
4. Respons upstream diterjemahkan kembali oleh `translate` (non-stream) atau
   dipetakan per event SSE oleh `ChatStreamState` (stream).
5. `instructions` dari upstream selalu dibuang (`strip_instructions`) agar prompt
   injeksi akun tidak bocor ke client.
