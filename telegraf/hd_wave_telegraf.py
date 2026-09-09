#!/usr/bin/env python3
"""Flatten hd_wave_query.py's array-of-values JSON for Telegraf's json parser."""

import sys
import os
import json

sys.path.insert(0, "/scripts")
from hd_wave_query import HdWaveQuery

PORT = int(os.environ.get("INVERTER_PORT", "1502"))
WANTED = {"ac_power", "dc_power", "temp_sink", "status_text", "status_vendor"}


def main():
    host = os.environ["INVERTER_HOST"]
    result = HdWaveQuery(host, PORT).read()
    flat = {v["name"]: v["value"] for v in result["values"] if v["name"] in WANTED}
    print(json.dumps(flat))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"hd_wave_telegraf: {e}", file=sys.stderr)
        sys.exit(1)
