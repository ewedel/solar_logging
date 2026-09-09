FROM telegraf:1.31

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-pip \
    && pip3 install --no-cache-dir --break-system-packages pyModbusTCP \
    && rm -rf /var/lib/apt/lists/*

COPY hd_wave_query.py telegraf/hd_wave_telegraf.py /scripts/
