# Solar Logging

Telegraf + InfluxDB 2.x + Grafana stack for a Raspberry Pi 4, logging:

- Every 15 minutes from the SolarEdge HD-Wave inverter (via [hd_wave_query.py](hd_wave_query.py)): `ac_power`, `dc_power`, `temp_sink`, `status_text`, `status_vendor`.
- Every 15 minutes from the Pi itself: CPU usage, memory usage, temperature. (Power consumption is not yet instrumented — see "Adding power consumption" below.)
- Every 15 minutes from the UPS (via the NUT service already running on the Pi for graceful shutdown): battery charge and line/battery status.
- Every 15 minutes, weather at the house (NWS station observations + Open-Meteo cloud cover and solar irradiance), for correlating with the inverter. The same service shows a tablet-friendly weather page on port 8080 — see "Weather" below.

Dashboard is served by Grafana on the LAN, viewable without logging in (read-only). InfluxDB is not exposed outside the Docker network.

## Files

| Path | Purpose |
|---|---|
| `hd_wave_query.py` | Existing Modbus TCP reader for the inverter. Unmodified; imported by the Telegraf wrapper. |
| `docker-compose.yml` | The four services: `influxdb`, `telegraf`, `grafana`, `weather`. |
| `.env.example` | Template for secrets/config. Copy to `.env` and fill in real values — `.env` is git-ignored. |
| `telegraf/Dockerfile` | Telegraf image + Python 3 + `pyModbusTCP`, so it can run the inverter poll itself. |
| `telegraf/telegraf.conf` | Collection config: inverter poll + Pi host metrics, both on a shared 15-minute interval. |
| `telegraf/hd_wave_telegraf.py` | Flattens `hd_wave_query.py`'s JSON into the shape Telegraf's parser expects. |
| `grafana/provisioning/` | Auto-configures the InfluxDB datasource and loads the starter dashboard on first boot. |
| `grafana/dashboards/solar_logging.json` | Starter dashboard: inverter panels, Pi health panels, UPS panels and a Weather row. |
| `weather/weather_service.py` | Weather collector (NWS + Open-Meteo, with station failover) and the web server for the tablet page and Telegraf's JSON. |
| `weather/static/index.html` | The tablet page: current conditions, wind compass rose, today's forecast, next 12 hours. |
| `weather/find_station.py` | Copy of `weather_station/find_station.py`, used to search for a replacement station when every configured one fails. Re-copy it here after changing the original. |
| `weather/Dockerfile` | `python:3.12-slim` + `requests`. |

## Deploying to the Pi

1. On the Pi, clone this repo (or `git pull` if already cloned).
2. `cp .env.example .env` and fill in real values:
   - `INFLUX_TOKEN` — make up any long random string; it's used to auto-provision InfluxDB on first boot and is then shared by Telegraf and Grafana.
   - `INFLUX_ADMIN_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` — pick real passwords.
   - `INVERTER_HOST` / `INVERTER_PORT` — set the inverter's actual address (default port may be ok as-is).
   - `WEATHER_*`, `SKY_STATION`, `NWS_USER_AGENT`, `PANEL_*` — see "Weather" below.
   - `NUT_TELEGRAF_PASSWORD` — pick a password; it must match the `[telegraf]` user you create in `/etc/nut/upsd.users` (see "Logging UPS status and battery charge" below). Only needed once you've set up NUT.
3. `docker compose up -d --build`
4. Browse to `http://<pi-ip>:3000` — dashboards are viewable immediately with no login (see "Anonymous read-only access" below). To make any change (edit a panel, add a data source), log in with `admin` / the `GRAFANA_ADMIN_PASSWORD` you set, **and change it immediately** (it's just seeded from `.env`, not a real per-user account).
5. Make sure Docker starts on boot: `sudo systemctl enable docker`.

The `Solar Logging` dashboard should be there already (provisioned automatically) — give it up to 15 minutes after first boot to show real data.

## Verifying it's working

- `docker compose ps` — all four containers should be `running`.
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

## Logging UPS status and battery charge

The NUT setup above (for graceful shutdown) already has `upsd` running natively on the Pi with live UPS data available via `upsc`. Telegraf has a built-in plugin for this — `inputs.upsd` (added in Telegraf v1.24.0; not to be confused with a plugin literally named "nut", which doesn't exist) — so getting battery charge and status into the same dashboard is mostly config, not new infrastructure.

1. **Add a second, read-only NUT user** for Telegraf, distinct from `upsmon`'s `monuser` and from the UPS's own SNMPv3 credentials — least-privilege, and lets you rotate/revoke it independently. Add to `/etc/nut/upsd.users`:
   ```
   [telegraf]
       password = <a new random password>
   ```
   (No `upsmon master` line needed here — that role is only for the shutdown-trigger client.)

2. **Make `upsd` reachable from the Telegraf container.** By default it only listens on `127.0.0.1`, which is fine for `upsmon`/`upsc` running on the same host, but not reachable from inside Docker. In `/etc/nut/upsd.conf`, **replace** the `LISTEN 127.0.0.1 3493` line with just:
   ```
   LISTEN 0.0.0.0 3493
   ```
   Don't keep both — `upsd` deliberately refuses to bind the wildcard address when a more specific one (`127.0.0.1`) is already bound on the same port (logged as `not listening on 0.0.0.0 port 3493`, not an error, just silently skipped), so having both lines leaves you back on `127.0.0.1` only. A single `0.0.0.0` binding already accepts connections to `127.0.0.1` too, so local `upsc`/`upsmon` keep working unchanged. This exposes the UPS monitor port to the LAN — the same trust boundary already accepted for Grafana's port 3000, not forwarded to the WAN. If you'd rather scope it tighter, bind to the Docker bridge network's gateway address instead of `0.0.0.0` (find it with `docker network inspect solar_logging_default | grep Gateway`).

3. Restart NUT's server: `sudo systemctl restart nut-server`.

4. Set `NUT_TELEGRAF_PASSWORD` in `.env` to the password from step 1, then `docker compose up -d` to pick up the compose and `telegraf.conf` changes (already included in this repo — see `[[inputs.upsd]]` in `telegraf/telegraf.conf`, and the `extra_hosts: host.docker.internal:host-gateway` entry on the `telegraf` service, which is what lets the container reach `upsd` on the Pi itself regardless of the Docker bridge subnet in use).

5. **What to expect.** `inputs.upsd` writes to measurement `upsd`, with `battery_charge_percent` as a numeric field. `docker compose logs telegraf` will show connection/auth errors from the `upsd` plugin if step 1 or 2 above didn't take (e.g. `dial tcp: connect: connection refused` means `upsd` isn't reachable yet, an auth-type error means the `[telegraf]` user/password don't match).

   Status is modeled unusually: rather than one `status` field/tag with a value like `"OL"`, Telegraf emits a **separate tag per active NUT status flag** — `status_OL="true"` while on line, `status_OB="true"` while on battery — and the tag for whichever flag *isn't* currently active is simply absent (not `"false"`). Confirmed by actually unplugging the UPS during testing, not from the plugin's docs (which described it differently and turned out to be inaccurate here). The shipped "UPS Status" panel handles this by querying the last known-OL point and last known-OB point separately, then keeping whichever is more recent (via Flux's `union()` + sort) — that way it reflects true current status even though the visible time range may span both a past "on line" period and a past "on battery" period. Only `OL`/`OB` are tracked; other NUT flags (`LB` low battery, `HB`, `RB`, etc.) aren't wired up — extend the panel's query with a third unioned branch the same way if you need one of those.

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

## Weather

The `weather` container polls the weather APIs on its own timers, keeps the latest values in memory (nothing is written to the SD card), and serves:

| URL | What |
|---|---|
| `http://<pi-ip>:8080/` | Tablet page: current conditions, wind compass rose, today's NWS forecast, next 12 hours, sunrise/sunset, active NWS alerts. Refreshes itself every minute. |
| `/api/current.json` | Everything the page shows. |
| `/api/obs.json`, `/api/model.json` | Flat JSON for Telegraf, which stores it as the `weather_obs` and `weather_model` measurements every 15 minutes. These return HTTP 503 when the service has nothing fresh, so no stale point gets logged. |

**Two data sources, each used for what it does best:**

- **NWS (api.weather.gov)**: measured temperature, humidity, dew point, wind, gusts and pressure from real stations; conditions text, cloud layers and visibility from an airport (ASOS) station; the forecast; alerts.
- **Open-Meteo**: cloud cover (total and low/mid/high), solar irradiance (horizontal GHI, and GTI on the plane of the panels from `PANEL_TILT`/`PANEL_AZIMUTH`), sunrise/sunset. It's also the last-resort fallback for every other value.

**Station choice and failover**, rechecked every 5 minutes:

1. `WEATHER_STATION` (LOAC1, the Los Altos RAWS station, the best match for the local microclimate) supplies the measurements, as long as its latest reading is under 90 minutes old and its temperature passed QC.
2. If it's stale or down, the stations in `WEATHER_FALLBACK_STATIONS` are tried in order (AW020, then KNUQ). A value the chosen station doesn't report (LOAC1 has no barometer) is filled in from the next usable station. Wind speed, gust and direction always come from one station together, so a calm reading never picks up a direction from elsewhere.
3. If every configured station fails and `WEATHER_FALLBACK_AUTO=true`, `find_station.py` searches for the best nearby station. The search makes many requests, so its answer is reused for 6 hours (1 hour if it found nothing).
4. If there's still nothing usable, the Open-Meteo model values are used and the point is tagged `station=model`.

LOAC1 and AW020 don't report conditions, cloud cover or visibility (only airport stations have a ceilometer and present-weather sensor), so those **sky fields** always come from `SKY_STATION` (KNUQ), or from Open-Meteo when KNUQ is stale. The primary station is tried first again on every cycle, so the service returns to it as soon as it recovers.

Every `weather_obs` point is tagged with `station`, `source` (`primary` / `fallback` / `auto` / `model`) and `sky_station`, and carries `obs_age_min`. The tablet page footer and the "Weather Station Used" panel in Grafana show which source is in use. NWS's station feed sometimes falls hours behind for every station at once; when that happens the page shows `model` until it catches up.

**Using it on a tablet:** open `http://<pi-ip>:8080/`, use the browser's "Add to Home screen" to get a full-screen app icon, and turn on the tablet's keep-screen-on (or the browser's stay-awake) setting. The page follows the tablet's light/dark setting. It needs no internet resources of its own: everything comes from the Pi.

**Checking it:**

- `docker compose logs -f weather`: each change of source is logged with the stations skipped and why.
- `curl http://<pi-ip>:8080/api/obs.json`: every number should have a decimal point (InfluxDB locks a field's type on its first write; see the UPS `force_float` note above).
- `docker compose logs -f telegraf`: the two `inputs.http` inputs should gather without errors on each 15-minute tick.

## Anonymous read-only access

Grafana is configured with `GF_AUTH_ANONYMOUS_ENABLED=true` and `GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer`, so anyone who can reach `http://<pi-ip>:3000` sees the dashboards immediately — no account, no password. Editing anything (panels, data sources, settings) still requires logging in as `admin`; a "Sign in" option remains available for that.

This means anyone on your LAN who can reach port 3000 can view the dashboards, same trust boundary already accepted for the rest of this stack (nothing here is meant to survive being exposed to the WAN — don't add a router port-forward for 3000). If you'd rather require a login even for viewing, remove the three `GF_AUTH_ANONYMOUS_*` lines from the `grafana` service in `docker-compose.yml` and re-run `docker compose up -d`.

If `GF_AUTH_ANONYMOUS_ORG_NAME` (`"Main Org."`) doesn't match your Grafana organization's actual name — e.g. if you renamed it — anonymous access won't attach to the right org; check under Administration → Organizations and update the compose file to match.

## Troubleshooting

- **Modbus connection errors in `telegraf` logs**: check `INVERTER_HOST`/`INVERTER_PORT` in `.env`, and that the inverter/DMZ firewall rule allowing port 1502 is still in place.
- **Grafana shows "no data" everywhere**: check the InfluxDB datasource's token matches what InfluxDB was actually seeded with. InfluxDB only applies `DOCKER_INFLUXDB_INIT_*` env vars on first run against an empty volume — if you change `INFLUX_TOKEN` in `.env` after that, it won't take effect until you wipe the `influxdb-data`/`influxdb-config` volumes and let it re-init.
- **`upsc` returns an auth/connection error**: double check the SNMPv3 credentials in `/etc/nut/ups.conf` match what's configured on the UPS's NMC, and that `snmpwalk` against the UPS works standalone before blaming NUT's config.
