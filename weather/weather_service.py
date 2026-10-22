#!/usr/bin/env python3
"""
weather_service.py - weather collector and tablet display for the solar logger.

One long-running process that:
  - polls NWS (station observations, gridpoint forecast, alerts) and
    Open-Meteo (cloud cover, solar irradiance, sun times) on its own timers,
    keeping everything in memory only (no extra SD-card writes);
  - serves a tablet page at / and JSON at /api/current.json;
  - serves flat JSON at /api/obs.json and /api/model.json for Telegraf's
    inputs.http, which writes them to InfluxDB on the stack's 15-minute grid.

Measured values come from WEATHER_STATION, with WEATHER_FALLBACK_STATIONS
(and optionally find_station.py's best_estimate) filling in when it is stale
or missing a value. Sky fields (conditions text, cloud cover, visibility)
come from SKY_STATION, an ASOS airport station, since mesonet and CWOP
stations don't report them. Open-Meteo is the last resort for both.

Configuration is from environment variables; see .env.example.
"""

import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from find_station import FindStation, parse_time, qc_flag


def _env_list(name):
    return [s.strip().upper() for s in os.environ.get(name, "").split(",") if s.strip()]


LAT = round(float(os.environ["WEATHER_LAT"]), 4)
LON = round(float(os.environ["WEATHER_LON"]), 4)
PRIMARY = os.environ.get("WEATHER_STATION", "").strip().upper()
FALLBACKS = [s for s in _env_list("WEATHER_FALLBACK_STATIONS") if s != PRIMARY]
SKY_STATION = os.environ.get("SKY_STATION", "").strip().upper()
FALLBACK_AUTO = os.environ.get("WEATHER_FALLBACK_AUTO", "true").lower() in ("1", "true", "yes")
USER_AGENT = os.environ.get("NWS_USER_AGENT", "(solar_logging weather service)")
PANEL_TILT = float(os.environ.get("PANEL_TILT", "20"))
PANEL_AZIMUTH = float(os.environ.get("PANEL_AZIMUTH", "0"))
HTTP_PORT = int(os.environ.get("WEATHER_PORT", "8080"))

STATIC_DIR = Path(__file__).resolve().parent / "static"

# find_station.py ships a placeholder User-Agent; NWS asks for real contact info
FindStation.NWS_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/geo+json"}

# A reading older than this is not used (same limit find_station.py uses)
STALE_AFTER_MINUTES = FindStation.STALE_AFTER_MINUTES
# Telegraf polls on the quarter hour; refresh Open-Meteo this long before that
MODEL_LEAD_SECONDS = 120
# How long a best_estimate() station choice (or "none found") is reused
AUTO_CACHE_HOURS = 6
AUTO_NONE_CACHE_HOURS = 1
# Endpoints for Telegraf return 503 when their data is older than this
MAX_SERVE_AGE_MINUTES = 30

# Fields a measurement station supplies; missing ones are filled down the chain
MEASURED_FIELDS = ("temp_f", "dewpoint_f", "rh_pct", "wind_mph", "wind_gust_mph",
                   "wind_dir_deg", "pressure_inhg", "feels_like_f")
# Wind fields always come together from one station: a calm reading has no
# direction and often no gust, which must not be filled from somewhere else
WIND_FIELDS = ("wind_mph", "wind_dir_deg", "wind_gust_mph")
SKY_FIELDS = ("conditions", "icon", "cloud_cover_pct", "visibility_mi")

# Cloud-layer cover codes as a percentage (midpoint of each okta range)
CLOUD_COVER_PCT = {"SKC": 0, "CLR": 0, "NSC": 0, "FEW": 19, "SCT": 44, "BKN": 75,
                   "OVC": 100, "VV": 100}

# WMO weather codes (Open-Meteo) -> (conditions text, icon key)
WMO_CODES = {
    0: ("Clear", "skc"), 1: ("Mostly Clear", "few"), 2: ("Partly Cloudy", "sct"),
    3: ("Overcast", "ovc"), 45: ("Fog", "fog"), 48: ("Freezing Fog", "fog"),
    51: ("Light Drizzle", "rain"), 53: ("Drizzle", "rain"), 55: ("Heavy Drizzle", "rain"),
    56: ("Freezing Drizzle", "fzra"), 57: ("Freezing Drizzle", "fzra"),
    61: ("Light Rain", "rain"), 63: ("Rain", "rain"), 65: ("Heavy Rain", "rain"),
    66: ("Freezing Rain", "fzra"), 67: ("Freezing Rain", "fzra"),
    71: ("Light Snow", "snow"), 73: ("Snow", "snow"), 75: ("Heavy Snow", "snow"),
    77: ("Snow Grains", "snow"), 80: ("Rain Showers", "rain_showers"),
    81: ("Rain Showers", "rain_showers"), 82: ("Heavy Rain Showers", "rain_showers"),
    85: ("Snow Showers", "snow"), 86: ("Snow Showers", "snow"),
    95: ("Thunderstorm", "tsra"), 96: ("Thunderstorm, Hail", "tsra"),
    99: ("Thunderstorm, Hail", "tsra"),
}


def log(message):
    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def _now():
    return datetime.now(timezone.utc)


def _epoch_ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


def _age_min(dt):
    return (_now() - dt).total_seconds() / 60 if dt else None


# ---------------------------------------------------------------------------
# Unit conversion of NWS values (all NWS observation values are SI)
# ---------------------------------------------------------------------------

def _c_to_f(c):
    return c * 9 / 5 + 32

CONVERT = {
    "wmoUnit:degC": _c_to_f,
    "wmoUnit:km_h-1": lambda v: v * 0.621371,
    "wmoUnit:m_s-1": lambda v: v * 2.236936,
    "wmoUnit:Pa": lambda v: v / 3386.389,
    "wmoUnit:m": lambda v: v / 1609.344,
}


def nws_value(props, name):
    """An NWS observation value converted to °F/mph/inHg/mi, or None if
    missing or rejected by QC (flag X)."""
    item = props.get(name) or {}
    value = item.get("value")
    if value is None or qc_flag(item) == "X":
        return None
    convert = CONVERT.get(item.get("unitCode"))
    return convert(value) if convert else value


def cloud_cover_from_layers(layers):
    """Total sky cover % from ASOS cloud layers: the most-covered layer wins."""
    covers = [CLOUD_COVER_PCT[l["amount"]] for l in layers or []
              if l.get("amount") in CLOUD_COVER_PCT]
    return max(covers) if covers else None


def icon_key(icon_url):
    """Short condition key from an NWS icon URL, e.g. '.../day/bkn?size=medium' -> 'bkn'.
    Combined icons ('rain,40/bkn') use the first part."""
    if not icon_url:
        return None
    parts = icon_url.split("?")[0].rstrip("/").split("/")
    # Two-part icons look like .../day/rain,40/bkn; the weather part is the earlier one
    key = parts[-1] if parts[-2] in ("day", "night") else parts[-2]
    return key.split(",")[0]


# ---------------------------------------------------------------------------
# API clients
# ---------------------------------------------------------------------------

class Nws:
    """api.weather.gov calls for one home point."""

    BASE = "https://api.weather.gov"
    TIMEOUT = 15

    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon
        self.point = None           # gridpoint URLs, from /points
        self.point_time = None

    def _get(self, url, params=None):
        res = requests.get(url, params=params, timeout=self.TIMEOUT,
                           headers=FindStation.NWS_HEADERS)
        res.raise_for_status()
        return res.json()

    def resolve_point(self, force=False):
        """Forecast/hourly/stations URLs for the home point (cached for a day)."""
        if (force or self.point is None
                or _now() - self.point_time > timedelta(hours=24)):
            props = self._get(f"{self.BASE}/points/{self.lat},{self.lon}")["properties"]
            rel = props.get("relativeLocation", {}).get("properties", {})
            self.point = {
                "forecast": props["forecast"],
                "hourly": props["forecastHourly"],
                "grid": f"{props.get('gridId')} {props.get('gridX')},{props.get('gridY')}",
                "place": ", ".join(p for p in (rel.get("city"), rel.get("state")) if p),
                "timezone": props.get("timeZone"),
            }
            self.point_time = _now()
            log(f"NWS gridpoint {self.point['grid']} ({self.point['place']})")
        return self.point

    def _gridpoint_get(self, key):
        """Fetch a gridpoint URL, re-resolving /points once if NWS moved it."""
        try:
            return self._get(self.resolve_point()[key])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return self._get(self.resolve_point(force=True)[key])
            raise

    def observation(self, station_id):
        """A station's latest reading, parsed. Raises on HTTP failure."""
        props = self._get(f"{self.BASE}/stations/{station_id}/observations/latest").get(
            "properties", {})
        obs_time = parse_time(props.get("timestamp"))
        temp_f = nws_value(props, "temperature")
        feels = nws_value(props, "heatIndex") or nws_value(props, "windChill")
        wind = nws_value(props, "windSpeed")
        direction = nws_value(props, "windDirection")
        return {
            "station": station_id,
            "time": obs_time,
            "age_min": _age_min(obs_time),
            "temp_f": temp_f,
            "dewpoint_f": nws_value(props, "dewpoint"),
            "rh_pct": nws_value(props, "relativeHumidity"),
            "wind_mph": wind,
            "wind_gust_mph": nws_value(props, "windGust"),
            # Direction is meaningless in calm air (ASOS reports 0)
            "wind_dir_deg": direction if wind else None,
            "pressure_inhg": nws_value(props, "barometricPressure"),
            "feels_like_f": feels if feels is not None else temp_f,
            "conditions": props.get("textDescription") or None,
            "icon": icon_key(props.get("icon")),
            "is_day": "/night/" not in (props.get("icon") or ""),
            "cloud_cover_pct": cloud_cover_from_layers(props.get("cloudLayers")),
            "visibility_mi": nws_value(props, "visibility"),
        }

    def station_name(self, station_id):
        return self._get(f"{self.BASE}/stations/{station_id}")["properties"].get("name")

    def forecast(self):
        """Next few named periods ('Today', 'Tonight', ...)."""
        periods = self._gridpoint_get("forecast")["properties"]["periods"]
        return [{
            "name": p["name"],
            "start": _epoch_ms(parse_time(p["startTime"])),
            "is_day": p.get("isDaytime"),
            "temp_f": p.get("temperature"),
            "pop_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
            "wind": f"{p.get('windDirection', '')} {p.get('windSpeed', '')}".strip(),
            "short": p.get("shortForecast"),
            "detailed": p.get("detailedForecast"),
            "icon": icon_key(p.get("icon")),
        } for p in periods[:4]]

    def hourly(self, hours=12):
        periods = self._gridpoint_get("hourly")["properties"]["periods"]
        now = _now() - timedelta(minutes=59)
        out = []
        for p in periods:
            start = parse_time(p["startTime"])
            if start < now:
                continue
            out.append({
                "start": _epoch_ms(start),
                "is_day": p.get("isDaytime"),
                "temp_f": p.get("temperature"),
                "pop_pct": (p.get("probabilityOfPrecipitation") or {}).get("value"),
                "wind": p.get("windSpeed"),
                "wind_dir": p.get("windDirection"),
                "short": p.get("shortForecast"),
                "icon": icon_key(p.get("icon")),
            })
            if len(out) >= hours:
                break
        return out

    def alerts(self):
        features = self._get(f"{self.BASE}/alerts/active",
                             params={"point": f"{self.lat},{self.lon}"}).get("features", [])
        return [{
            "event": f["properties"].get("event"),
            "headline": f["properties"].get("headline"),
            "severity": f["properties"].get("severity"),
            "ends": _epoch_ms(parse_time(f["properties"].get("ends")
                                         or f["properties"].get("expires"))),
        } for f in features]


class OpenMeteo:
    """Open-Meteo model data at the home point."""

    URL = "https://api.open-meteo.com/v1/forecast"
    TIMEOUT = 15
    CURRENT = ("temperature_2m,relative_humidity_2m,apparent_temperature,dew_point_2m,"
               "is_day,precipitation,weather_code,cloud_cover,cloud_cover_low,"
               "cloud_cover_mid,cloud_cover_high,wind_speed_10m,wind_direction_10m,"
               "wind_gusts_10m,surface_pressure,shortwave_radiation,"
               "direct_normal_irradiance,diffuse_radiation,global_tilted_irradiance")

    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon

    def fetch(self):
        res = requests.get(self.URL, timeout=self.TIMEOUT, params={
            "latitude": self.lat, "longitude": self.lon,
            "current": self.CURRENT,
            "hourly": "cloud_cover,precipitation_probability,temperature_2m",
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min",
            "tilt": PANEL_TILT, "azimuth": PANEL_AZIMUTH,
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "precipitation_unit": "inch", "timezone": "auto", "forecast_days": 2,
        })
        res.raise_for_status()
        data = res.json()
        offset = timedelta(seconds=data.get("utc_offset_seconds", 0))

        def local_to_utc(s):
            # Open-Meteo times are local wall time without an offset
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc) - offset

        c = data["current"]
        code = c.get("weather_code")
        text, icon = WMO_CODES.get(code, (None, None))
        current = {
            # 'time' is the start of the 15-minute step the values describe
            "time": local_to_utc(c["time"]),
            "temp_f": c.get("temperature_2m"),
            "feels_like_f": c.get("apparent_temperature"),
            "dewpoint_f": c.get("dew_point_2m"),
            "rh_pct": c.get("relative_humidity_2m"),
            "wind_mph": c.get("wind_speed_10m"),
            "wind_gust_mph": c.get("wind_gusts_10m"),
            "wind_dir_deg": c.get("wind_direction_10m"),
            "pressure_inhg": (c["surface_pressure"] / 33.86389
                              if c.get("surface_pressure") is not None else None),
            "precip_in": c.get("precipitation"),
            "is_day": c.get("is_day"),
            "weather_code": code,
            "conditions": text,
            "icon": icon,
            "cloud_cover_pct": c.get("cloud_cover"),
            "cloud_low_pct": c.get("cloud_cover_low"),
            "cloud_mid_pct": c.get("cloud_cover_mid"),
            "cloud_high_pct": c.get("cloud_cover_high"),
            "ghi_wm2": c.get("shortwave_radiation"),
            "dni_wm2": c.get("direct_normal_irradiance"),
            "dhi_wm2": c.get("diffuse_radiation"),
            "gti_wm2": c.get("global_tilted_irradiance"),
        }
        h = data.get("hourly", {})
        hourly = [{"start": _epoch_ms(local_to_utc(t)), "cloud_cover_pct": cc,
                   "pop_pct": pop, "temp_f": tf}
                  for t, cc, pop, tf in zip(h.get("time", []), h.get("cloud_cover", []),
                                            h.get("precipitation_probability", []),
                                            h.get("temperature_2m", []))]
        d = data.get("daily", {})
        daily = [{"date": day, "sunrise": _epoch_ms(local_to_utc(sr)),
                  "sunset": _epoch_ms(local_to_utc(ss)), "hi_f": hi, "lo_f": lo}
                 for day, sr, ss, hi, lo in zip(d.get("time", []), d.get("sunrise", []),
                                                d.get("sunset", []),
                                                d.get("temperature_2m_max", []),
                                                d.get("temperature_2m_min", []))]
        return {"current": current, "hourly": hourly, "daily": daily}


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class Collector:
    """Keeps the latest weather in memory, refreshed on per-source timers."""

    OBS_EVERY = timedelta(minutes=5)
    FORECAST_EVERY = timedelta(minutes=30)
    ALERTS_EVERY = timedelta(minutes=10)
    LOOP_SECONDS = 20

    def __init__(self):
        self.nws = Nws(LAT, LON)
        self.meteo = OpenMeteo(LAT, LON)
        self.lock = threading.Lock()
        self.obs = None            # merged observation (see refresh_obs)
        self.model = None          # Open-Meteo result
        self.forecast = []
        self.hourly = []
        self.alerts = []
        self.errors = {}           # source -> last error text
        self.names = {}            # station id -> name
        self.auto = None           # (station id or None, chosen at)
        self.last_sources = None   # (station, sky station), to log only changes
        # 5-minute samples for the pressure trend and the wind-direction trail
        self.history = deque(maxlen=48)
        self.due = {}

    # ----- timers ------------------------------------------------------------

    def run_forever(self):
        while True:
            try:
                self.tick()
            except Exception as e:      # keep the loop alive whatever happens
                log(f"collector error: {e!r}")
            time.sleep(self.LOOP_SECONDS)

    def tick(self):
        now = _now()
        # Model first, so the observation chain can fall back to it
        if now >= self.due.get("model", now):
            self._guard("model", self.refresh_model)
            self.due["model"] = self._next_model_time(now)
        for name, every, refresh in (("obs", self.OBS_EVERY, self.refresh_obs),
                                     ("forecast", self.FORECAST_EVERY, self.refresh_forecast),
                                     ("alerts", self.ALERTS_EVERY, self.refresh_alerts)):
            if now >= self.due.get(name, now):
                self._guard(name, refresh)
                self.due[name] = now + every

    @staticmethod
    def _next_model_time(now):
        """MODEL_LEAD_SECONDS before the next quarter hour (Telegraf's poll)."""
        quarter = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        nxt = quarter + timedelta(minutes=15) - timedelta(seconds=MODEL_LEAD_SECONDS)
        while nxt <= now + timedelta(seconds=30):
            nxt += timedelta(minutes=15)
        return nxt

    def _guard(self, name, refresh):
        try:
            refresh()
            self.errors.pop(name, None)
        except Exception as e:
            self.errors[name] = str(e)
            log(f"{name} refresh failed: {e}")

    # ----- refreshers ----------------------------------------------------------

    def refresh_model(self):
        model = self.meteo.fetch()
        with self.lock:
            self.model = model

    def refresh_forecast(self):
        forecast, hourly = self.nws.forecast(), self.nws.hourly()
        with self.lock:
            self.forecast, self.hourly = forecast, hourly

    def refresh_alerts(self):
        alerts = self.nws.alerts()
        with self.lock:
            self.alerts = alerts

    def _fetch_station(self, sid, cache, skipped):
        """Latest obs for a station (once per refresh), or None with a skip reason."""
        if sid in cache:
            return cache[sid]
        obs = None
        try:
            obs = self.nws.observation(sid)
            if obs["age_min"] is None or obs["age_min"] > STALE_AFTER_MINUTES:
                skipped.append((sid, f"reading is {self._age_text(obs['age_min'])} old"))
                obs = None
            elif obs["temp_f"] is None:
                skipped.append((sid, "no usable temperature"))
                obs = None
        except requests.RequestException as e:
            skipped.append((sid, f"request failed: {e}"))
        cache[sid] = obs
        if obs and sid not in self.names:
            try:
                self.names[sid] = self.nws.station_name(sid)
            except requests.RequestException:
                pass
        return obs

    @staticmethod
    def _age_text(minutes):
        if minutes is None:
            return "an unknown time"
        return f"{minutes:.0f} min" if minutes < 120 else f"{minutes / 60:.1f} h"

    def _auto_station(self, exclude, skipped):
        """find_station.py's best station, cached (see AUTO_CACHE_HOURS)."""
        if self.auto:
            sid, chosen_at = self.auto
            keep = AUTO_CACHE_HOURS if sid else AUTO_NONE_CACHE_HOURS
            if _now() - chosen_at < timedelta(hours=keep):
                return sid
        log("searching for a nearby station with find_station.py")
        try:
            best = FindStation(lat=LAT, long=LON, exclude=exclude, quiet=True).best_estimate()
            sid = best["station_id"] if best["source"] == "station" else None
            if sid:
                self.names[sid] = best.get("station_name")
            log(f"find_station.py chose {sid or 'no station'}")
        except Exception as e:
            sid = None
            skipped.append(("auto", f"station search failed: {e}"))
        self.auto = (sid, _now())
        return sid

    def refresh_obs(self):
        """
        Build the merged observation:
          - measured fields from the first usable station in the chain
            (primary, configured fallbacks, then find_station.py's choice),
            with any field it lacks filled from later usable stations;
          - sky fields from SKY_STATION, or the model;
          - anything still missing from the Open-Meteo model.
        """
        cache, skipped = {}, []
        chain = [PRIMARY] + FALLBACKS
        merged, sources = {}, {}
        main, main_role = None, None

        def absorb(obs, role):
            nonlocal main, main_role
            if main is None:
                main, main_role = obs, role
            # A field counts as filled once it has a source, even when that
            # source's value is None (e.g. no wind direction in calm air)
            if "wind_mph" not in sources and obs.get("wind_mph") is not None:
                for f in WIND_FIELDS:
                    merged[f], sources[f] = obs[f], obs["station"]
            for f in MEASURED_FIELDS:
                if f not in sources and obs.get(f) is not None:
                    merged[f], sources[f] = obs[f], obs["station"]

        for i, sid in enumerate(chain):
            obs = self._fetch_station(sid, cache, skipped)
            if obs:
                absorb(obs, "primary" if i == 0 else "fallback")
            if all(f in sources for f in MEASURED_FIELDS):
                break

        if main is None and FALLBACK_AUTO:
            sid = self._auto_station(chain, skipped)
            if sid and sid not in chain:
                obs = self._fetch_station(sid, cache, skipped)
                if obs:
                    absorb(obs, "auto")

        model = (self.model or {}).get("current")
        model_ok = model and _age_min(model["time"]) is not None \
            and _age_min(model["time"]) < MAX_SERVE_AGE_MINUTES + 15

        # Sky fields: the airport station, else the model
        sky = self._fetch_station(SKY_STATION, cache, skipped) if SKY_STATION else None
        sky_station = None
        if sky and sky.get("conditions"):
            sky_station = SKY_STATION
            for f in SKY_FIELDS:
                merged[f] = sky.get(f)
                sources[f] = SKY_STATION
            merged["is_day"] = sky.get("is_day")
        if model_ok:
            if sky_station is None:
                sky_station = "model"
                merged["is_day"] = bool(model["is_day"])
            if "wind_mph" not in sources:
                for f in WIND_FIELDS:
                    merged[f], sources[f] = model.get(f), "model"
            for f in SKY_FIELDS + MEASURED_FIELDS:
                if f not in sources and model.get(f) is not None:
                    merged[f], sources[f] = model[f], "model"

        if main is None and not model_ok:
            with self.lock:
                self.obs = None
            log("no usable observation from any station or the model")
            return

        merged.update({
            "station": main["station"] if main else "model",
            "station_name": self.names.get(main["station"]) if main else "Open-Meteo model",
            "source": main_role or "model",
            "sky_station": sky_station or "none",
            "obs_time": main["time"] if main else model["time"],
            "obs_age_min": main["age_min"] if main else _age_min(model["time"]),
            "field_sources": sources,
            "skipped": skipped,
            "updated": _now(),
        })
        if (merged["station"], merged["sky_station"]) != self.last_sources:
            self.last_sources = (merged["station"], merged["sky_station"])
            log(f"measurements from {merged['station']} ({merged['source']}), "
                f"sky from {merged['sky_station']}; skipped: {skipped or 'none'}")

        with self.lock:
            self.obs = merged
            self.history.append({"t": _now(), "pressure_inhg": merged.get("pressure_inhg"),
                                 "wind_dir_deg": merged.get("wind_dir_deg"),
                                 "wind_mph": merged.get("wind_mph")})

    # ----- views -------------------------------------------------------------------

    def pressure_trend(self):
        """Pressure change over about 3 hours (inHg), from the in-memory samples."""
        if not self.obs or self.obs.get("pressure_inhg") is None:
            return None
        cutoff = _now() - timedelta(hours=2, minutes=45)
        old = [h for h in self.history if h["t"] <= cutoff and h["pressure_inhg"] is not None]
        return self.obs["pressure_inhg"] - old[-1]["pressure_inhg"] if old else None

    def obs_for_telegraf(self):
        """Flat weather_obs point, or None when there's nothing fresh to log."""
        with self.lock:
            obs = self.obs
        if not obs or _age_min(obs["updated"]) > MAX_SERVE_AGE_MINUTES:
            return None
        point = {"station": obs["station"], "source": obs["source"],
                 "sky_station": obs["sky_station"]}
        for f in ("temp_f", "dewpoint_f", "rh_pct", "wind_mph", "wind_gust_mph",
                  "wind_dir_deg", "pressure_inhg", "visibility_mi", "cloud_cover_pct",
                  "feels_like_f", "obs_age_min"):
            if obs.get(f) is not None:
                # Always float: InfluxDB locks a field's type on first write
                point[f] = round(float(obs[f]), 3)
        if obs.get("conditions"):
            point["conditions"] = obs["conditions"]
        return point

    def model_for_telegraf(self):
        """Flat weather_model point, or None when the model data is stale."""
        with self.lock:
            model = self.model
        if not model:
            return None
        c = model["current"]
        if _age_min(c["time"]) > MAX_SERVE_AGE_MINUTES + 15:
            return None
        point = {}
        for f in ("cloud_cover_pct", "cloud_low_pct", "cloud_mid_pct", "cloud_high_pct",
                  "ghi_wm2", "dni_wm2", "dhi_wm2", "gti_wm2", "temp_f", "rh_pct",
                  "wind_mph", "wind_gust_mph", "wind_dir_deg", "precip_in", "is_day",
                  "weather_code"):
            if c.get(f) is not None:
                point[f] = round(float(c[f]), 3)
        return point

    def current_view(self):
        """Everything the tablet page shows."""
        with self.lock:
            obs, model = self.obs, self.model
            view = {
                "now": _epoch_ms(_now()),
                "place": (self.nws.point or {}).get("place"),
                "forecast": self.forecast,
                "hourly": self.hourly,
                "alerts": self.alerts,
                "errors": dict(self.errors),
                "wind_trail": [{"t": _epoch_ms(h["t"]), "dir": h["wind_dir_deg"],
                                "mph": h["wind_mph"]}
                               for h in self.history
                               if h["t"] > _now() - timedelta(hours=1)
                               and h["wind_dir_deg"] is not None],
            }
        if obs:
            view["obs"] = {k: v for k, v in obs.items()
                           if k not in ("obs_time", "updated", "skipped")}
            view["obs"]["obs_time"] = _epoch_ms(obs["obs_time"])
            view["obs"]["updated"] = _epoch_ms(obs["updated"])
            view["obs"]["skipped"] = [{"station": s, "reason": r} for s, r in obs["skipped"]]
            view["obs"]["pressure_trend"] = self.pressure_trend()
        if model:
            c = model["current"]
            view["solar"] = {k: c.get(k) for k in ("ghi_wm2", "gti_wm2", "dni_wm2",
                                                   "dhi_wm2", "cloud_low_pct",
                                                   "cloud_mid_pct", "cloud_high_pct",
                                                   "cloud_cover_pct")}
            view["solar"]["time"] = _epoch_ms(c["time"])
            view["daily"] = model["daily"]
            view["model_hourly"] = model["hourly"]
        return view


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    collector = None

    def log_message(self, fmt, *args):
        pass    # Telegraf and the tablet poll constantly; keep the log quiet

    def _send(self, status, body, content_type):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload):
        if payload is None:
            self._send(503, '{"error": "no fresh data"}', "application/json")
        else:
            self._send(200, json.dumps(payload), "application/json")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/obs.json":
            self._json(self.collector.obs_for_telegraf())
        elif path == "/api/model.json":
            self._json(self.collector.model_for_telegraf())
        elif path == "/api/current.json":
            self._json(self.collector.current_view())
        elif path in ("/", "/index.html"):
            self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/manifest.json":
            self._send(200, json.dumps({
                "name": "Weather", "short_name": "Weather", "start_url": "/",
                "display": "fullscreen", "background_color": "#0e1116",
                "theme_color": "#0e1116"}), "application/manifest+json")
        else:
            self._send(404, "not found", "text/plain")


def main():
    if not PRIMARY:
        sys.exit("WEATHER_STATION is not set")
    log(f"weather service: {LAT},{LON} station {PRIMARY}, fallbacks {FALLBACKS or 'none'}, "
        f"sky {SKY_STATION or 'model'}, auto fallback {'on' if FALLBACK_AUTO else 'off'}")
    collector = Collector()
    threading.Thread(target=collector.run_forever, daemon=True).start()
    Handler.collector = collector
    server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    log(f"serving on port {HTTP_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
