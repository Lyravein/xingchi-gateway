#!/usr/bin/env python3
"""Entry point gateway: muat konfigurasi lalu jalankan server.

Tidak berisi logika protokol atau terjemahan; semuanya ada di paket.
"""

from xingchi_gateway.server import run

if __name__ == "__main__":
    raise SystemExit(run())
