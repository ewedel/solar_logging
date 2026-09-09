# Solar Logging

Telegraf + InfluxDB 2.x + Grafana stack for a Raspberry Pi 4, logging:

- Every 15 minutes from the SolarEdge HD-Wave inverter (via [hd_wave_query.py](hd_wave_query.py)): `ac_power`, `dc_power`, `temp_sink`, `status_text`, `status_vendor`.
- Every 15 minutes from the Pi itself: CPU usage, memory usage, temperature. (Power consumption is not yet instrumented — see "Adding power consumption" below.)

Dashboard is served by Grafana on the LAN. InfluxDB is not exposed outside the Docker network.

## Files

| Path | Purpose |
|---|---|
| `hd_wave_query.py` | Existing Modbus TCP reader for the inverter. Unmodified; imported by the Telegraf wrapper. |
| `docker-compose.yml` | The three services: `influxdb`, `telegraf`, `grafana`. |
| `.env.example` | Template for secrets/config. Copy to `.env` and fill in real values — `.env` is git-ignored. |
| `telegraf/Dockerfile` | Telegraf image + Python 3 + `pyModbusTCP`, so it can run the inverter poll itself. |
| `telegraf/telegraf.conf` | Collection config: inverter poll + Pi host metrics, both on a shared 15-minute interval. |
| `telegraf/hd_wave_telegraf.py` | Flattens `hd_wave_query.py`'s JSON into the shape Telegraf's parser expects. |
| `grafana/provisioning/` | Auto-configures the InfluxDB datasource and loads the starter dashboard on first boot. |
| `grafana/dashboards/solar_logging.json` | Starter dashboard: inverter panels + Pi health panels. |

## Deploying to the Pi

1. On the Pi, clone this repo (or `git pull` if already cloned).
2. `cp .env.example .env` and fill in real values:
   - `INFLUX_TOKEN` — make up any long random string; it's used to auto-provision InfluxDB on first boot and is then shared by Telegraf and Grafana.
   - `INFLUX_ADMIN_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` — pick real passwords.
   - `INVERTER_HOST` / `INVERTER_PORT` — set the inverter's actual address (default port may be ok as-is).
3. `docker compose up -d --build`
4. Browse to `http://<pi-ip>:3000`, log in with `admin` / the `GRAFANA_ADMIN_PASSWORD` you set, **and change it immediately** (it's just seeded from `.env`, not a real per-user account).
5. Make sure Docker starts on boot: `sudo systemctl enable docker`.

The `Solar Logging` dashboard should be there already (provisioned automatically) — give it up to 15 minutes after first boot to show real data.

## Verifying it's working

- `docker compose ps` — all three containers should be `running`.
- `docker compose logs -f telegraf` — watch for errors from the inverter poll (a Modbus connection failure — wrong host/port, inverter or DMZ firewall down — shows up here immediately) and confirm both the inverter and Pi-metrics inputs are flushing every 15 minutes.
- In Grafana: Settings → Data Sources → InfluxDB should show a green "datasource is working" check.
- Restart the stack (`docker compose down && docker compose up -d`) and confirm InfluxDB still has its data and Grafana still has its dashboard — both come from named Docker volumes, not the container filesystem.

## Handling power loss / unclean shutdown

The Pi is on a UPS, but UPS capacity is finite, so an extended outage still ends in a hard power cut unless the Pi shuts itself down first.

**Already handled by this stack's design, no action needed:**
- InfluxDB 2.x's storage engine is WAL-based and crash-tolerant — an unclean stop just replays the WAL on next start. Worst case: losing the single most recent (≤15 minute) sample, not the database.
- Raspberry Pi OS's default ext4 filesystem is journaled and recovers from a hard power cut without a full corruption/reformat.
- All three containers have `restart: unless-stopped`, so after any reboot — clean or forced — the stack comes back up on its own once Docker starts.

**Graceful early shutdown via NUT (Network UPS Tools):** the UPS (CyberPower CP1500PFCLCD) has a network management card reachable at `192.168.1.60`. NUT can poll it over SNMP and trigger a clean `shutdown -h now` on the Pi before the battery actually runs out — much better than riding it to zero.

This runs **natively on the Pi OS, not in Docker** — `upsmon`'s whole job is shutting down the host, which would need `--privileged` + a host PID namespace to do safely from a container.

Setup on the Pi:

```bash
sudo apt install nut nut-snmp
```

On the UPS's own web UI (`http://192.168.1.60`), SNMPv3 is already enabled with a read-only user `monitorx` (auth-only, SHA, no encryption). SNMPv1 is left disabled — NUT only needs one protocol.

`/etc/nut/nut.conf`:
```
MODE=standalone
```

`/etc/nut/ups.conf`:
```
[cyberpower]
    driver = snmp-ups
    port = 192.168.1.60
    snmp_version = v3
    secLevel = authNoPriv
    secName = monitorx
    authProtocol = SHA
    authPassword = <the configured passphrase>
    mibs = cyberpower
```
Keep this file `chmod 640`, owned by the `nut` user — the passphrase never goes in `.env` or the git repo. Before wiring up `upsmon`, confirm it actually works:
```bash
snmpwalk -v3 -u monitorx -l authNoPriv -a SHA -A '<passphrase>' 192.168.1.60 .1.3.6.1.2.1.33
```
If that comes back empty, try `mibs = ietf` instead of `cyberpower` — the generic UPS-MIB mapping, in case this NMC firmware doesn't support CyberPower's vendor extensions well. SNMPv1 remains a documented fallback only if v3 turns out to be flaky on this card's firmware.

`/etc/nut/upsd.users` (a fresh local password, unrelated to the UPS's own SNMP credentials):
```
[monuser]
    password = <generate a new local password>
    upsmon master
```

`/etc/nut/upsmon.conf`:
```
MONITOR cyberpower@localhost 1 monuser <password> master
SHUTDOWNCMD "/sbin/shutdown -h now"
```
The actual low-battery threshold is configured on the UPS itself (its NMC lets you set the "low battery" runtime/percentage) — leave it with a reasonable safety margin (e.g. 20-30% remaining) rather than 0%.

Enable at boot:
```bash
sudo systemctl enable --now nut-server nut-monitor
```

Verify:
```bash
sudo systemctl status nut-server nut-monitor
upsc cyberpower@localhost
```
`upsc` should return live battery charge/load/status, not a connection or auth error.

**Important caveat:** this only works if the network path between the Pi and the UPS's management card stays powered during the outage. If the switch between them isn't itself on a UPS, the Pi loses visibility into the UPS's status exactly when it matters most — worth checking now, not after the first real outage. Also worth testing the shutdown path deliberately (a controlled test, not waiting for a real outage) before trusting it.

## Adding power consumption later

The Pi 4 has no built-in power sensor, so this metric is deliberately skipped for now — the dashboard has an empty placeholder panel for it. To add it:

1. Wire up an INA219 or INA260 I2C power-monitor HAT.
2. Write a small reader script (e.g. using `adafruit-circuitpython-ina219`) that prints flat JSON like `{"power_w": 12.3}`, following the exact same pattern as `telegraf/hd_wave_telegraf.py`.
3. Add a second `[[inputs.exec]]` block to `telegraf.conf` pointing at it.
4. Pass the I2C device through to the Telegraf container in `docker-compose.yml`:
   ```yaml
   devices:
     - "/dev/i2c-1:/dev/i2c-1"
   ```
5. Point the placeholder panel in the dashboard at the new field.

## Troubleshooting

- **Modbus connection errors in `telegraf` logs**: check `INVERTER_HOST`/`INVERTER_PORT` in `.env`, and that the inverter/DMZ firewall rule allowing port 1502 is still in place.
- **Grafana shows "no data" everywhere**: check the InfluxDB datasource's token matches what InfluxDB was actually seeded with. InfluxDB only applies `DOCKER_INFLUXDB_INIT_*` env vars on first run against an empty volume — if you change `INFLUX_TOKEN` in `.env` after that, it won't take effect until you wipe the `influxdb-data`/`influxdb-config` volumes and let it re-init.
- **`upsc` returns an auth/connection error**: double check the SNMPv3 credentials in `/etc/nut/ups.conf` match what's configured on the UPS's NMC, and that `snmpwalk` against the UPS works standalone before blaming NUT's config.
