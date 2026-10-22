"""
find_station.py - find nearby NWS weather stations for a US address and
compare each station's own latest reading with Open-Meteo's model estimate,
along with per-station quality (MADIS QC flags, network type) and a health
grade from the station's last 24 hours of reports.

Usage:
    python3 find_station.py                          # sample address
    python3 find_station.py "2101 Shattuck Ave, Berkeley, CA 94704"
    python3 find_station.py --adjacent               # also check the 8 neighbouring NWS grid points
    python3 find_station.py --debug                  # show the history behind each health grade
    python3 find_station.py --radius 10 --max-stations 3
    python3 find_station.py --best --exclude KNUQ    # fallback: one best reading, skipping KNUQ
    python3 find_station.py --best --json            # the same, as JSON for other programs

From Python (an address, or both lat and long, is required):
    from find_station import FindStation
    finder = FindStation("1023 Tulane Drive, Mountain View, CA 94040", adjacent=True)
    rows = finder.run()          # prints the report and returns one dict per station

    best = FindStation(lat=37.3764, long=-122.0929, exclude=["KNUQ"],
                       quiet=True).best_estimate()

Microclimate notes:
1. Topographical variations: each station's own coordinates are passed to
   Open-Meteo's v1/forecast endpoint, which returns the model's terrain
   elevation there. Comparing it with the station's real elevation shows
   whether the model "sees" the station's valley floor or ridgeline.
2. Marine layer / inversions: comparing the measured readings across
   stations (and against the model) shows features such as thermal
   inversions or a sharp coastal marine layer.
"""

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

import requests


# Short descriptions of the NWS/MADIS network (provider) codes, printed in the
# legend for the networks that appear in the table. Sources: MADIS surface
# provider lists (madis.ncep.noaa.gov).
NETWORK_INFO = {
    "ASOS":       "Automated Surface Observing System at airports (NWS/FAA). "
                  "Maintained sensors, hourly reports plus specials; the most reliable.",
    "ASOS-HFM":   "High-frequency (about 5-minute) ASOS reports; same airport sensors as ASOS.",
    "HF-METAR":   "High-frequency ASOS data from the FAA; same airport sensors as ASOS.",
    "OTHER-MTR":  "Airport reports (METARs) from non-ASOS systems, e.g. AWOS. "
                  "Maintained, but may report less often.",
    "OTHER-HFM":  "High-frequency airport reports from non-ASOS systems.",
    "NonFedAWOS": "Non-federal AWOS airport stations (state or local).",
    "APRSWXNET":  "Citizen Weather Observer Program (CWOP): volunteer home weather "
                  "stations. Siting and upkeep vary widely.",
    "RAWS":       "MADIS 'Cooperative Mesonets in the Western U.S.' feed via MesoWest: "
                  "fire-weather RAWS plus agency/state networks. Usually hourly.",
    "COOP":       "NWS Cooperative Observer Program stations.",
}

class GeocodeError(Exception):
    """The address could not be matched by the Census geocoder."""


# Address used by the command line when none is given
SAMPLE_ADDRESS = "1023 Tulane Drive, Mountain View, CA 94040"

# Short labels for the grid points checked with --adjacent
GRID_POINT_ABBREV = {
    "Center": "C", "North": "N", "South": "S", "East": "E", "West": "W",
    "NorthEast": "NE", "NorthWest": "NW", "SouthEast": "SE", "SouthWest": "SW",
}


# ---------------------------------------------------------------------------
# Small stateless helpers (parsing and formatting)
# ---------------------------------------------------------------------------

def qc_flag(item):
    """
    Return the MADIS QC letter for one observation value, normalised so that
    "qc:V" and "V" both come back as "V". None if there's no flag.
    """
    flag = (item or {}).get('qualityControl')
    return flag.split(':')[-1].upper() if flag else None


def display_flag(item):
    """QC letter for the table: '-' when the value itself is missing."""
    if not item or item.get('value') is None:
        return "-"
    return qc_flag(item) or "?"


def parse_time(timestamp):
    """Parse an NWS ISO timestamp into an aware UTC datetime (or None)."""
    if not timestamp:
        return None
    return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))


def whole_degree_shares(values_c):
    """Share of °C values that are whole °C, and share that are whole °F."""
    if not values_c:
        return 0.0, 0.0
    near = lambda x: abs(x - round(x)) < 0.01
    whole_c = sum(1 for v in values_c if near(v)) / len(values_c)
    whole_f = sum(1 for v in values_c if near(v * 9 / 5 + 32)) / len(values_c)
    return whole_c, whole_f


def is_whole_degree(values_c):
    """True if a station mostly reports whole °C or whole °F."""
    whole_c, whole_f = whole_degree_shares(values_c)
    return max(whole_c, whole_f) >= 0.8


def _local(t):
    """Short local-time label for debug output, e.g. '10-19 22:53'."""
    return t.astimezone().strftime('%m-%d %H:%M')


def _num(value, fmt, missing="N/A"):
    """Format a number, or show a placeholder when it's missing."""
    return format(value, fmt) if value is not None else missing


def _pct(value):
    """Format a humidity percentage, or N/A when it's missing."""
    return f"{value:.0f}%" if value is not None else "N/A"


def _age(minutes):
    """Readable age of an observation, e.g. '12m' or '3.5h'."""
    if minutes is None:
        return "N/A"
    return f"{minutes:.0f}m" if minutes < 120 else f"{minutes / 60:.1f}h"


# ---------------------------------------------------------------------------
# The station finder
# ---------------------------------------------------------------------------

class FindStation:
    """
    Find the NWS observation stations nearest a target point (from a US
    address or given coordinates), and report for each one:
      - Open-Meteo's model estimate at the station vs. the station's own
        latest NWS reading
      - its network type, MADIS QC flags, reading age and a health grade

    With adjacent=True, the 8 NWS grid points around the target (found with
    get_adjacent_coordinates) are checked too, and their nearest stations are
    added to the report.
    """

    # NWS API requires a descriptive User-Agent header
    NWS_HEADERS = {
        'User-Agent': '(myweatherapp.com, contact@myweatherapp.com)',
        'Accept': 'application/geo+json'
    }

    # Seconds to wait for each HTTP request before giving up
    TIMEOUT = 15
    # A station reading older than this is flagged as stale
    STALE_AFTER_MINUTES = 90
    # Health grade window
    HISTORY_HOURS = 24
    # Same temperature for this long looks like a stuck sensor. Stations that
    # report whole degrees (KNUQ in whole °C, CWOP/RAWS often in whole °F) can
    # sit on one value for hours on a calm night, so they get longer.
    STUCK_HOURS = 4
    STUCK_HOURS_COARSE = 8
    # Pause between stations, to be polite to the free public APIs
    PAUSE_SECONDS = 0.3
    # Indent of each station's second line, so it lines up after "[XXXXX] "
    ROW2_INDENT = " " * 8
    # Best estimate: with 3 or more usable stations, skip any whose temperature
    # is this far from their median (catches a sun-baked or failing sensor)
    OUTLIER_F = 6.0
    # With fewer than 3 usable stations there's no meaningful median, so a
    # reading is instead skipped if it's this far from the model at the station
    # (generous, since real microclimates can differ from the model)
    MODEL_OUTLIER_F = 15.0

    def __init__(self, address=None, lat=None, long=None, max_radius_miles=15,
                 max_stations=5, adjacent=False, debug=False, exclude=(), quiet=False):
        """
        Give either an address (geocoded with the US Census geocoder) or
        both lat and long. Raises ValueError if neither is given.

        max_radius_miles: ignore stations farther than this from a grid point
        max_stations:     nearest stations kept per grid point
        adjacent:         also check the 8 neighbouring NWS grid points
        debug:            print the history details behind each health grade
        exclude:          station IDs to leave out, e.g. ["KNUQ"] when that is
                          the preconfigured station that didn't respond
        quiet:            don't print progress messages (for use as a fallback)
        """
        self.address = address
        self.lat = lat
        self.long = long
        self.max_radius_miles = max_radius_miles
        self.max_stations = max_stations
        self.adjacent = adjacent
        self.debug = debug
        self.exclude = {sid.strip().upper() for sid in exclude if sid.strip()}
        self.quiet = quiet
        self.grid_points = []   # filled in by find_stations()
        if not self.address and (self.lat is None or self.long is None):
            raise ValueError("FindStation needs a mailing address, or both lat and long.")

    def _say(self, message):
        """Progress message (suppressed when quiet)."""
        if not self.quiet:
            print(message)

    # ----- HTTP -------------------------------------------------------------

    def _get(self, url, params=None, nws=True):
        """GET a URL (with the NWS headers for NWS calls) and return the response."""
        res = requests.get(url, params=params,
                           headers=self.NWS_HEADERS if nws else None,
                           timeout=self.TIMEOUT)
        res.raise_for_status()
        return res

    # ----- Main entry point ---------------------------------------------------

    def run(self):
        """Geocode if needed, gather every station's data, print the report
        (ending with the best estimate). Returns the list of per-station rows."""
        rows = self.gather()
        self.print_report(rows)
        self.print_best_estimate(self.best_estimate(rows))
        return rows

    def gather(self):
        """Geocode if needed, find the stations and fetch all their data."""
        if self.lat is None or self.long is None:
            self.lat, self.long = self.geocode(self.address)
            self._say(f"-> Found Coordinates: Lat {self.lat}, Long {self.long}\n")

        stations = self.find_stations()
        self._say(f"\nFound {len(stations)} stations. Fetching their data...")
        return self.collect(stations)

    # ----- Best estimate ---------------------------------------------------------

    def best_estimate(self, rows=None):
        """
        Pick the single most trustworthy current reading near the target.
        Typical use is as a fallback when a preconfigured station doesn't
        respond:

            best = FindStation(address, exclude=["KNUQ"], quiet=True).best_estimate()

        Rules, in order:
          1. Usable stations have a current temperature, a reading no older
             than STALE_AFTER_MINUTES, and a health grade of good or fair
             (stations whose health check failed count as fair).
          2. With 3 or more usable stations, any reading more than OUTLIER_F
             from their median is skipped as a likely bad sensor. With fewer,
             a reading more than MODEL_OUTLIER_F from the model at that
             station is skipped instead.
          3. Prefer health "good" over "fair", then the nearest station.
          4. If no station qualifies, fall back to Open-Meteo's model value at
             the target itself (source "model").

        Returns a dict with source, temperature, humidity and the station
        details, plus "skipped": the stations passed over and why.
        """
        if rows is None:
            rows = self.gather()

        usable, skipped = [], []
        for r in rows:
            sid, obs, health = r['station']['id'], r['obs'], r['health']
            grade = health['grade'] if health else "fair"
            if obs['temp_f'] is None:
                skipped.append((sid, "no current temperature"))
            elif obs['age_min'] is None or obs['age_min'] > self.STALE_AFTER_MINUTES:
                skipped.append((sid, f"reading is {_age(obs['age_min'])} old"))
            elif grade == "poor":
                skipped.append((sid, "health poor: " + "; ".join(health['reasons'])))
            else:
                usable.append((r, grade))

        consensus = median(r['obs']['temp_f'] for r, _ in usable) if usable else None
        if len(usable) >= 3:
            kept = []
            for r, grade in usable:
                off = r['obs']['temp_f'] - consensus
                if abs(off) > self.OUTLIER_F:
                    skipped.append((r['station']['id'],
                                    f"{off:+.1f} °F from the nearby median ({consensus:.1f} °F)"))
                else:
                    kept.append((r, grade))
            usable = kept
        else:
            kept = []
            for r, grade in usable:
                if r['delta'] is not None and abs(r['delta']) > self.MODEL_OUTLIER_F:
                    skipped.append((r['station']['id'],
                                    f"{r['delta']:+.1f} °F from the model at the station"))
                else:
                    kept.append((r, grade))
            usable = kept

        if usable:
            r, grade = min(usable, key=lambda u: (u[1] != "good", u[0]['station']['distance']))
            stn, obs = r['station'], r['obs']
            return {
                "source": "station",
                "station_id": stn['id'],
                "station_name": stn['name'],
                "network": r['network'],
                "distance_mi": round(stn['distance'], 2),
                "temp_f": round(obs['temp_f'], 1),
                "humidity": round(obs['humidity']) if obs['humidity'] is not None else None,
                "observed_at": obs.get('time'),
                "age_min": round(obs['age_min']),
                "health": grade,
                "nearby_median_f": round(consensus, 1) if consensus is not None else None,
                "skipped": skipped,
            }

        # No usable station: use the model at the target itself
        try:
            model = self.get_model_metrics(self.lat, self.long)
        except requests.RequestException:
            model = {"temp_f": None, "humidity": None, "time": None}
        return {
            "source": "model",
            "station_id": None,
            "station_name": None,
            "network": "Open-Meteo model",
            "distance_mi": 0.0,
            "temp_f": model['temp_f'],
            "humidity": model['humidity'],
            "observed_at": model.get('time'),
            "age_min": None,
            "health": None,
            "nearby_median_f": None,
            "skipped": skipped,
        }

    @staticmethod
    def format_best_estimate(best):
        """One-line summary of a best_estimate() result."""
        rh = f", {best['humidity']}% RH" if best['humidity'] is not None else ""
        temp = _num(best['temp_f'], '.1f')
        if best['source'] == "station":
            return (f"{temp} °F{rh} from [{best['station_id']}] {best['station_name']} "
                    f"({best['network']}, {best['distance_mi']:.1f} mi, "
                    f"{_age(best['age_min'])} old, health {best['health']})")
        return (f"{temp} °F{rh} from the Open-Meteo model at the target "
                f"(no usable station; model time {best['observed_at']} UTC)")

    def print_best_estimate(self, best, blank_line=True):
        """Print the best estimate and the stations it passed over."""
        print(("\n" if blank_line else "") + "Best estimate: " + self.format_best_estimate(best))
        for sid, reason in best['skipped']:
            print(f"   skipped [{sid}]: {reason}")

    # ----- Location -----------------------------------------------------------

    def geocode(self, address_string):
        """
        Geocode a US street address with the free US Census Bureau geocoder
        (same approach as nws_sample_query1_fixed.py).
        """
        self._say(f"Geocoding address: '{address_string}'...")
        res = self._get("https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
                        params={"address": address_string,
                                "benchmark": "Public_AR_Current",
                                "format": "json"},
                        nws=False)
        matches = res.json().get("result", {}).get("addressMatches", [])
        if not matches:
            raise GeocodeError("No address matches found. Please check the address format.")

        # Census returns x = longitude, y = latitude
        coordinates = matches[0]["coordinates"]
        self._say(f"-> Matched: {matches[0].get('matchedAddress', address_string)}")
        # NWS recommends limiting coordinates to 4 decimal places
        return round(coordinates["y"], 4), round(coordinates["x"], 4)

    @staticmethod
    def calculate_distance(lat1, long1, lat2, long2):
        """Haversine formula to compute distance in miles between two coordinates."""
        R = 3958.8  # Earth radius in miles
        d_lat = math.radians(lat2 - lat1)
        d_long = math.radians(long2 - long1)
        a = (math.sin(d_lat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_long / 2) ** 2)
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    @staticmethod
    def get_adjacent_coordinates(lat, long):
        """
        A 3x3 cluster of points around the target, one NWS grid cell
        (about 2.5 x 2.5 km) apart: the center plus its 8 neighbours.
        """
        # 2.5 km is ~0.0225 degrees of latitude everywhere, but a degree of
        # longitude shrinks with cos(latitude), so the east/west step is wider.
        lat_offset = 0.0225
        long_offset = lat_offset / math.cos(math.radians(lat))
        return {
            "Center":    (lat, long),
            "North":     (lat + lat_offset, long),
            "South":     (lat - lat_offset, long),
            "East":      (lat, long + long_offset),
            "West":      (lat, long - long_offset),
            "NorthEast": (lat + lat_offset, long + long_offset),
            "NorthWest": (lat + lat_offset, long - long_offset),
            "SouthEast": (lat - lat_offset, long + long_offset),
            "SouthWest": (lat - lat_offset, long - long_offset),
        }

    # ----- Stations -----------------------------------------------------------

    def get_grid_point(self, lat, long):
        """The NWS forecast grid cell for a point, and its station-list URL."""
        res = self._get(f"https://api.weather.gov/points/{round(lat, 4)},{round(long, 4)}")
        props = res.json()['properties']
        return {
            "grid": f"{props.get('gridId', '?')} {props.get('gridX', '?')},{props.get('gridY', '?')}",
            "stations_url": props['observationStations'],
        }

    def get_grid_stations(self, stations_url):
        """The observation stations NWS assigns to a grid cell (GeoJSON features)."""
        return self._get(stations_url).json()['features']

    def find_stations(self):
        """
        Check the center grid point (and with adjacent=True its 8 neighbours),
        keep each point's nearest stations within the radius, and merge them.
        Each station records which grid points found it ("via").
        Results are sorted by distance from the target.
        """
        if self.adjacent:
            points = self.get_adjacent_coordinates(self.lat, self.long)
            self._say(f"Querying NWS grid stations for {len(points)} grid points "
                  f"around {self.lat}, {self.long}...")
        else:
            points = {"Center": (self.lat, self.long)}
            self._say(f"Querying NWS grid stations for Location: {self.lat}, {self.long}...")

        stations = {}        # station id -> station dict
        list_cache = {}      # station-list URL -> features (neighbours often share one)
        self.grid_points = []
        for label, (p_lat, p_long) in points.items():
            point = {"label": label, "lat": p_lat, "long": p_long,
                     "grid": "?", "stations": [], "error": None}
            self.grid_points.append(point)
            try:
                cell = self.get_grid_point(p_lat, p_long)
                point["grid"] = cell["grid"]
                if cell["stations_url"] not in list_cache:
                    list_cache[cell["stations_url"]] = self.get_grid_stations(cell["stations_url"])
                features = list_cache[cell["stations_url"]]
            except (requests.RequestException, KeyError, ValueError) as e:
                point["error"] = str(e)
                continue

            # Nearest stations to this grid point, within the radius
            nearby = []
            for feat in features:
                props = feat['properties']
                s_long, s_lat = feat['geometry']['coordinates'][:2]
                d = self.calculate_distance(p_lat, p_long, s_lat, s_long)
                if d <= self.max_radius_miles:
                    nearby.append((d, props, s_lat, s_long))
            nearby.sort(key=lambda n: n[0])

            nearby = [n for n in nearby if n[1]['stationIdentifier'].upper() not in self.exclude]
            for _, props, s_lat, s_long in nearby[:self.max_stations]:
                sid = props['stationIdentifier']
                point["stations"].append(sid)
                if sid not in stations:
                    stations[sid] = {
                        "id": sid,
                        "name": props['name'],
                        "lat": s_lat,
                        "long": s_long,
                        # Distance from the target (not from the grid point)
                        "distance": self.calculate_distance(self.lat, self.long, s_lat, s_long),
                        # The station's real elevation, to compare with the model's terrain
                        "elevation_m": (props.get('elevation') or {}).get('value'),
                        "props": props,
                        "via": [],
                    }
                stations[sid]["via"].append(label)

        return sorted(stations.values(), key=lambda s: s['distance'])

    def get_network(self, station_id, listed_props):
        """
        The station's network/provider (e.g. ASOS, RAWS), with its sub-provider
        when there is one. Uses the station-list entry if it already has it,
        otherwise asks the station endpoint.
        """
        props = listed_props
        if not props.get('provider'):
            props = self._get(f"https://api.weather.gov/stations/{station_id}").json().get('properties', {})
        provider = props.get('provider') or "?"
        sub = props.get('subProvider')
        return f"{provider}/{sub}" if sub and sub != provider else provider

    # ----- Readings -------------------------------------------------------------

    def get_model_metrics(self, lat, long):
        """Open-Meteo's current model temperature/humidity and terrain elevation at a point."""
        res = self._get("https://api.open-meteo.com/v1/forecast",
                        params={"latitude": lat, "longitude": long,
                                "current": "temperature_2m,relative_humidity_2m",
                                "temperature_unit": "fahrenheit"},
                        nws=False)
        data = res.json()
        current = data.get('current', {})
        return {
            "temp_f": current.get('temperature_2m'),
            "humidity": current.get('relative_humidity_2m'),
            "elevation_m": data.get('elevation'),   # Model terrain elevation
            "time": current.get('time'),             # Model time step, in UTC
        }

    def get_observation(self, station_id):
        """
        The station's own latest NWS reading. Values that failed QC (flag "X")
        are dropped, and the reading's age is returned so stale reports show.
        """
        props = self._get(f"https://api.weather.gov/stations/{station_id}/observations/latest"
                          ).json().get('properties', {})

        qc_failed = False

        def checked_value(name):
            nonlocal qc_failed
            item = props.get(name) or {}
            if qc_flag(item) == 'X':
                qc_failed = True
                return None
            return item.get('value')

        temp_c = checked_value('temperature')
        humidity = checked_value('relativeHumidity')

        obs_time = parse_time(props.get('timestamp'))
        age_min = ((datetime.now(timezone.utc) - obs_time).total_seconds() / 60
                   if obs_time else None)

        return {
            "temp_f": temp_c * 9 / 5 + 32 if temp_c is not None else None,
            "humidity": humidity,
            "age_min": age_min,
            "qc_failed": qc_failed,
            "time": props.get('timestamp'),
            # MADIS QC letters for temperature / humidity, e.g. "V/S"
            "qc_temp": display_flag(props.get('temperature')),
            "qc_rh": display_flag(props.get('relativeHumidity')),
        }

    # ----- Health -----------------------------------------------------------------

    def get_history(self, station_id, start):
        """
        The station's reports since `start`. Follows NWS "next" page links,
        but stops once a page reaches back past `start` (NWS otherwise keeps
        paging back about a week). Returns (features, debug lines).
        """
        res = self._get(f"https://api.weather.gov/stations/{station_id}/observations",
                        params={"start": start.strftime('%Y-%m-%dT%H:%M:%SZ'), "limit": 500})
        data = res.json()
        features = data.get('features', [])
        dbg = [f"request: {res.url}",
               f"page 1: {len(features)} reports returned (HTTP {res.status_code})"]

        def reaches_start(page):
            return any((parse_time((f.get('properties') or {}).get('timestamp'))
                        or start) < start for f in page)

        reached = reaches_start(features)
        for _ in range(5):
            next_url = (data.get('pagination') or {}).get('next')
            if reached or not next_url:
                break
            data = self._get(next_url).json()
            page = data.get('features', [])
            dbg.append(f"next page: {len(page)} reports returned")
            if not page:
                break
            features.extend(page)
            reached = reaches_start(page)
        if reached:
            dbg.append("stopped paging: reached reports older than the window")
        elif not (data.get('pagination') or {}).get('next'):
            dbg.append("no further pages offered by NWS")
        else:
            dbg.append("stopped following pages after the cap; more may exist")
        return features, dbg

    def get_health(self, station_id, latest_age_min):
        """
        Grade a station's health from its last HISTORY_HOURS of reports:
          - missing:  share of reports with no temperature value
          - flagged:  share of temperatures MADIS questioned (Q), rejected (X)
                      or marked bad (B)
          - stuck:    longest stretch with an unchanged temperature
                      (longer allowance for whole-degree stations)
          - stale:    latest reading older than STALE_AFTER_MINUTES
        Hours-with-reports is returned for the table but not graded: the NWS
        history has archive gaps shared by many stations at once.
        """
        start = datetime.now(timezone.utc) - timedelta(hours=self.HISTORY_HOURS)
        features, dbg = self.get_history(station_id, start)

        by_time = {}   # keyed by timestamp so a repeated report is only counted once
        for feat in features:
            props = feat.get('properties', {})
            t = parse_time(props.get('timestamp'))
            if t and t >= start:
                by_time[t] = props.get('temperature') or {}
        reports = sorted(by_time.items(), key=lambda r: r[0])

        if self.debug:
            dbg.extend(self._history_debug(features, reports, start))

        hours_with_reports = len({int((t - start).total_seconds() // 3600) for t, _ in reports})

        temps = [(t, item) for t, item in reports if item.get('value') is not None]
        missing = 1 - len(temps) / len(reports) if reports else 1.0
        flagged = (sum(1 for _, item in temps if qc_flag(item) in ('Q', 'X', 'B')) / len(temps)
                   if temps else 0.0)
        stuck_h = self._longest_unchanged(temps)[0]
        coarse = is_whole_degree([item['value'] for _, item in temps])
        stuck_limit = self.STUCK_HOURS_COARSE if coarse else self.STUCK_HOURS
        stale = latest_age_min is None or latest_age_min > self.STALE_AFTER_MINUTES

        poor, fair = [], []
        if missing >= 0.5:
            poor.append(f"temperature missing in {missing:.0%} of reports")
        elif missing >= 0.1:
            fair.append(f"temperature missing in {missing:.0%} of reports")
        if flagged >= 0.25:
            poor.append(f"{flagged:.0%} of temperatures questioned/rejected by QC")
        elif flagged >= 0.05:
            fair.append(f"{flagged:.0%} of temperatures questioned/rejected by QC")
        if stuck_h >= stuck_limit:
            fair.append(f"temperature unchanged for {stuck_h:.1f} h, possible stuck sensor")
        if stale:
            poor.append("latest reading is stale or missing")

        return {
            "grade": "poor" if poor else "fair" if fair else "good",
            "hours": hours_with_reports,
            "missing": missing,
            "flagged": flagged,
            "stuck_h": stuck_h,
            "reasons": poor + fair,
            "debug": dbg,
        }

    @staticmethod
    def _longest_unchanged(temps):
        """
        Longest run of an unchanged temperature: (hours, start, end, value °C).
        A run restarts when the value changes or after a gap of over 90 minutes
        in the history (we can't tell what the sensor did while reports were missing).
        """
        best = (0.0, None, None, None)
        run_start = None
        for i, (t, item) in enumerate(temps):
            if (i == 0 or item['value'] != temps[i - 1][1]['value']
                    or (t - temps[i - 1][0]).total_seconds() > 90 * 60):
                run_start = t
            length = (t - run_start).total_seconds() / 3600
            if best[1] is None or length > best[0]:
                best = (length, run_start, t, item['value'])
        return best

    def _history_debug(self, features, reports, start):
        """Debug lines describing the history behind the health grade."""
        lines = []
        all_times = sorted(t for t in (parse_time((f.get('properties') or {}).get('timestamp'))
                                       for f in features) if t)
        lines.append(f"returned {len(features)} reports in total; {len(reports)} unique within "
                     f"the window (since {_local(start)}); "
                     f"{sum(1 for t in all_times if t < start)} older than the window")
        if all_times:
            lines.append(f"oldest returned {_local(all_times[0])}, "
                         f"newest {_local(all_times[-1])} (local time)")

        # Reports per hour, oldest hour first: '.' = none, 1-9 = count, '+' = 10 or more
        counts = Counter(int((t - start).total_seconds() // 3600) for t, _ in reports)
        hist = "".join("." if counts[h] == 0 else str(counts[h]) if counts[h] < 10 else "+"
                       for h in range(self.HISTORY_HOURS))
        lines.append(f"reports per hour from {_local(start)}: [{hist}]")

        gaps = [(a, b) for (a, _), (b, _) in zip(reports, reports[1:])
                if (b - a).total_seconds() > 90 * 60]
        if gaps:
            shown = ", ".join(f"{_local(a)} to {_local(b)} ({(b - a).total_seconds() / 3600:.1f} h)"
                              for a, b in gaps[:6])
            more = f" (+{len(gaps) - 6} more)" if len(gaps) > 6 else ""
            lines.append(f"gaps over 90 min: {shown}{more}")
        else:
            lines.append("no gaps over 90 min between reports")

        temps = [(t, item) for t, item in reports if item.get('value') is not None]
        if temps:
            values = [item['value'] for _, item in temps]
            whole_c, whole_f = whole_degree_shares(values)
            lines.append(f"{len(values)} temperatures, {len(set(values))} distinct values, "
                         f"{whole_c:.0%} whole °C, {whole_f:.0%} whole °F")
            flags = Counter(qc_flag(item) or "none" for _, item in temps)
            lines.append("QC flags: " + ", ".join(f"{k}={n}" for k, n in flags.most_common()))
            hours, run_from, run_to, value = self._longest_unchanged(temps)
            lines.append(f"longest unchanged temperature: {value:.2f} °C "
                         f"({value * 9 / 5 + 32:.1f} °F) from "
                         f"{_local(run_from)} to {_local(run_to)} ({hours:.1f} h)")
        else:
            lines.append("no temperature values in the window")
        return lines

    # ----- Gather and report ---------------------------------------------------

    def collect(self, stations):
        """Fetch network, model, latest reading and health for each station.
        A failed lookup blanks only that part of the station's row."""
        rows = []
        for stn in stations:
            notes = []

            try:
                network = self.get_network(stn['id'], stn['props'])
            except requests.RequestException:
                network = "?"

            try:
                model = self.get_model_metrics(stn['lat'], stn['long'])
            except requests.RequestException as e:
                notes.append(f"Open-Meteo request failed: {e}")
                model = {"temp_f": None, "humidity": None, "elevation_m": None, "time": None}

            try:
                obs = self.get_observation(stn['id'])
            except requests.RequestException as e:
                notes.append(f"NWS observation request failed: {e}")
                obs = {"temp_f": None, "humidity": None, "age_min": None, "qc_failed": False,
                       "time": None, "qc_temp": "-", "qc_rh": "-"}

            try:
                health = self.get_health(stn['id'], obs['age_min'])
            except requests.RequestException as e:
                health = None
                notes.append(f"health check failed: {e}")

            # Measured minus model: a large gap points to a local microclimate,
            # or to a badly sited sensor.
            delta = (obs['temp_f'] - model['temp_f']
                     if obs['temp_f'] is not None and model['temp_f'] is not None else None)
            stale = obs['age_min'] is not None and obs['age_min'] > self.STALE_AFTER_MINUTES

            rows.append({
                "station": stn, "network": network, "model": model, "obs": obs,
                "health": health, "delta": delta, "stale": stale, "notes": notes,
            })
            time.sleep(self.PAUSE_SECONDS)
        return rows

    def print_report(self, rows):
        """Print the two-line-per-station table and its legend."""
        if self.adjacent:
            self._print_grid_points()

        # Grid points that answered; a station found by all of them shows "all"
        ok_points = [p['label'] for p in self.grid_points if not p['error']]

        # Second-line text for every station first, so its columns can be sized
        line2 = []
        for r in rows:
            via = r['station']['via']
            via_str = ("all" if len(ok_points) > 1 and set(via) >= set(ok_points)
                       else ",".join(GRID_POINT_ABBREV.get(v, v) for v in via))
            h = r['health']
            health_str = "?" if h is None else h['grade'] + (
                " (" + "; ".join(h['reasons']) + ")" if h['reasons'] else "")
            line2.append({
                "network": r['network'][:16],
                "qc": f"{r['obs']['qc_temp']}/{r['obs']['qc_rh']}",
                "age": _age(r['obs']['age_min']) + (" *" if r['stale'] else ""),
                "hist": "?" if h is None else f"{h['hours']}/{self.HISTORY_HOURS}h",
                "via": via_str,
                "health": health_str,
            })
        width = lambda key, title: max([len(title)] + [len(l[key]) for l in line2])
        w_net, w_age = width("network", "Network"), width("age", "Obs age")
        w_hist, w_via = width("hist", "NWS hist"), width("via", "Via")

        def second_line(net, qc, age, hist, via, health):
            via_part = f"{via:<{w_via}} | " if self.adjacent else ""
            return (f"{self.ROW2_INDENT}{net:<{w_net}} | {qc:<4} | {age:<{w_age}} | "
                    f"{hist:<{w_hist}} | {via_part}{health}")

        print("Model = Open-Meteo estimate at the station's location; "
              "Obs = the station's own latest NWS reading.\n")
        header = (f"{'Station ID & Name':<34} | {'Dist':>7} | {'Elev m':>7} | "
                  f"{'Mdl °F':>6} | {'Obs °F':>7} | {'Δ °F':>6} | "
                  f"{'Mdl RH':>6} | {'Obs RH':>6}")
        print(header)
        print(second_line("Network", "QC", "Obs age", "NWS hist", "Via", "Health (reasons)"))
        print("=" * len(header))

        for r, l2 in zip(rows, line2):
            stn, model, obs = r['station'], r['model'], r['obs']
            title = f"[{stn['id']}] {stn['name']}"[:34]
            elev = f"{_num(stn['elevation_m'], '.0f', '?')}/{_num(model['elevation_m'], '.0f', '?')}"
            obs_t = _num(obs['temp_f'], ".1f") + (" x" if obs['qc_failed'] else "")
            # Line 1: readings
            print(f"{title:<34} | {stn['distance']:4.1f} mi | {elev:>7} | "
                  f"{_num(model['temp_f'], '.1f'):>6} | {obs_t:>7} | {_num(r['delta'], '+.1f', ''):>6} | "
                  f"{_pct(model['humidity']):>6} | {_pct(obs['humidity']):>6}")
            # Line 2: station quality and health
            print(second_line(l2['network'], l2['qc'], l2['age'], l2['hist'], l2['via'], l2['health']))
            for note in r['notes']:
                print(f"{self.ROW2_INDENT}  note: {note}")
            if self.debug and r['health']:
                for line in r['health']['debug']:
                    print(f"{self.ROW2_INDENT}  debug: {line}")
            print("-" * len(header))

        self._print_legend(rows)

    def _print_grid_points(self):
        """With adjacent=True: which grid points were checked and what each found."""
        print("\nGrid points checked (NWS grid cell, nearest stations to that point):")
        w_label = max(len(p['label']) for p in self.grid_points)
        for p in self.grid_points:
            abbrev = GRID_POINT_ABBREV.get(p['label'], p['label'])
            found = (f"failed: {p['error']}" if p['error']
                     else " ".join(p['stations']) or "no stations within radius")
            print(f"   {abbrev:<2} {p['label']:<{w_label}}  {p['lat']:.4f}, {p['long']:.4f}  "
                  f"{p['grid']:<10}  {found}")
        print()

    def _print_legend(self, rows):
        print()
        if self.exclude:
            print("Excluded stations: " + ", ".join(sorted(self.exclude)))
        model_time = next((r['model']['time'] for r in rows if r['model'].get('time')), None)
        if model_time:
            print(f"Model values valid for {model_time} UTC (Open-Meteo 'current' step).")
        if any(r['stale'] for r in rows):
            print(f"* Station reading is older than {self.STALE_AFTER_MINUTES} minutes; "
                  "treat it as out of date.")
        if any(r['obs']['qc_failed'] for r in rows):
            print("x A station value failed NWS quality control and was left out.")
        print("Elev m = station / model elevation.  "
              "QC = MADIS flag for temperature/humidity in the latest reading:")
        print("   V verified, S screened, C coarse pass, Z not checked, "
              "Q questioned, X rejected, G/B manually good/bad, - no value.")
        codes = sorted({r['network'].split('/')[0] for r in rows})
        if codes:
            w_code = max(len(c) for c in codes)
            print("Network types in this table:")
            for code in codes:
                info = NETWORK_INFO.get(code, "No description on file for this network code.")
                print(f"   {code:<{w_code}}  {info}")
        print(f"NWS hist = hours of the last {self.HISTORY_HOURS} with reports in the NWS history "
              "(often has archive gaps, so shown for information only).")
        if self.adjacent:
            print("Via = grid point(s) whose nearest stations include this one: C center, "
                  "N/S/E/W and NE/NW/SE/SW neighbours (one NWS grid cell, ~2.5 km, away); "
                  "'all' = every grid point checked.")
        print(f"Health = graded on the last {self.HISTORY_HOURS} h of reports "
              "(missing temps, Q/X flags, stuck values) and the latest reading's age.")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def _ensure_utf8_output():
    """
    The table uses characters such as "Δ" and "°". When output is redirected
    to a file on Windows, Python writes it in the legacy code page (cp1252),
    which has no "Δ", so printing fails. Switch to UTF-8 in that case.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            "Δ°".encode(stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def main(argv=None):
    _ensure_utf8_output()
    parser = argparse.ArgumentParser(
        description="Find nearby NWS weather stations for a US address and compare "
                    "their readings with Open-Meteo's model.")
    parser.add_argument("address", nargs="*",
                        help=f"US street address (default: {SAMPLE_ADDRESS})")
    parser.add_argument("--adjacent", action="store_true",
                        help="also check the 8 neighbouring NWS grid points and report their stations")
    parser.add_argument("--debug", action="store_true",
                        help="print the history details behind each station's health grade")
    parser.add_argument("--radius", type=float, default=15,
                        help="ignore stations farther than this many miles from a grid point (default 15)")
    parser.add_argument("--max-stations", type=int, default=5,
                        help="nearest stations to keep per grid point (default 5)")
    parser.add_argument("--exclude", action="append", default=[], metavar="IDS",
                        help="station ID(s) to leave out, comma-separated or repeated "
                             "(e.g. the preconfigured station that didn't respond)")
    parser.add_argument("--best", action="store_true",
                        help="print only the best estimate (no table or progress messages)")
    parser.add_argument("--json", action="store_true",
                        help="with --best: print the best estimate as JSON, for other programs")
    args = parser.parse_args(argv)
    if args.json and not args.best:
        parser.error("--json needs --best")

    exclude = [sid for item in args.exclude for sid in item.split(",")]
    finder = FindStation(address=" ".join(args.address) or SAMPLE_ADDRESS,
                         max_radius_miles=args.radius, max_stations=args.max_stations,
                         adjacent=args.adjacent, debug=args.debug,
                         exclude=exclude, quiet=args.best)
    try:
        if args.best:
            best = finder.best_estimate()
            if args.json:
                print(json.dumps(best, indent=2))
            else:
                finder.print_best_estimate(best, blank_line=False)
        else:
            finder.run()
    except GeocodeError as err:        # the address didn't match
        print(f"\nCould not geocode the address: {err}")
        return 1
    except requests.RequestException as err:
        print(f"\nRequest failed: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
