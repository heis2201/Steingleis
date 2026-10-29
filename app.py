import datetime
import json
import os
import re
import tempfile
import urllib.parse
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import altair as alt

# ---------------------------------------------------------
# ANWENDUNGS-VERSION & GRUNDKONFIGURATION
# ---------------------------------------------------------
APP_VERSION = "0.1.7"

st.set_page_config(
    page_title=f"BÜ Steingleis Monitor v{APP_VERSION}",
    page_icon="🚦",
    layout="wide"
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

def get_german_now() -> datetime.datetime:
    """Liefert die exakte deutsche Ortszeit ohne Tzinfo-Tags."""
    return datetime.datetime.now(BERLIN_TZ).replace(tzinfo=None)

# Robuster absoluter Dateipfad
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATION_NAME = "Neustadt(Weinstr)Hbf"

CONFIG_SCHEMA = {
    # Taktung & API-Schonung
    "ui_refresh_sec": (int, 2, 30, 5),
    "api_cache_local_sec": (int, 10, 120, 60),
    "api_cache_through_sec": (int, 15, 180, 120),
    # Stellwerkszeiten
    "lead_dep": (int, 0, 600, 240),
    "lag_dep": (int, 0, 300, 90),
    "lead_arr": (int, 0, 600, 160),
    "lag_arr": (int, 0, 300, 40),
    "min_gap": (int, 0, 300, 30),
    # Fernverkehr & Audio
    "track_through_trains": (bool, None, None, True),
    "transit_ma_min": (int, 5, 60, 16),
    "transit_kl_min": (int, 5, 60, 19),
    "sound_alarm": (bool, None, None, True),
    "sound_warn_sec": (int, 10, 180, 45)
}

DEFAULT_CONFIG = {k: v[3] for k, v in CONFIG_SCHEMA.items()}

def parse_bool_value(value: Any, default: bool = False) -> bool:
    """Robustes Parsen von Boolean-Konfigurationswerten aus JSON/Strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "ja"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "nein", ""}:
            return False
    return bool(default)

def validate_and_sanitize_config(raw_cfg: dict) -> dict:
    if not isinstance(raw_cfg, dict):
        raw_cfg = {}

    clean = {}
    for key, (v_type, v_min, v_max, default_val) in CONFIG_SCHEMA.items():
        val = raw_cfg.get(key, default_val)
        try:
            if v_type == bool:
                clean[key] = parse_bool_value(val, bool(default_val))
            elif v_type == int:
                v = int(val)
                if v_min is not None and v < v_min:
                    v = v_min
                elif v_max is not None and v > v_max:
                    v = v_max
                clean[key] = v
        except (ValueError, TypeError):
            clean[key] = default_val
    return clean

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return validate_and_sanitize_config(data)
        except Exception:
            st.session_state["config_load_error"] = True
            return DEFAULT_CONFIG.copy()
    return DEFAULT_CONFIG.copy()

def save_config_atomic(cfg: dict, filepath: str = CONFIG_FILE) -> bool:
    try:
        clean_cfg = validate_and_sanitize_config(cfg)
        dir_name = os.path.dirname(os.path.abspath(filepath))
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(clean_cfg, tf, indent=2)
            temp_name = tf.name
        os.replace(temp_name, filepath)
        return True
    except Exception:
        return False

if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()

# Alarm-Entprellung
if "last_alarmed_closure" not in st.session_state:
    st.session_state.last_alarmed_closure = None

WEST_CORRIDOR_KEYWORDS = [
    "lambrecht", "neidenfels", "weidenthal", "frankenstein", "hochspeyer",
    "kaiserslautern", "landstuhl", "homburg", "saarbrücken", "saarbruecken",
    "st. ingbert", "saarlouis", "merzig", "trier", "koblenz", "paris",
    "kusel", "pirmasens"
]

WEST_BOUND_DESTS = ["kaiserslautern", "saarbrücken", "saarbruecken", "paris", "homburg", "landstuhl"]
EAST_BOUND_DESTS = ["mannheim", "ludwigshafen", "frankfurt", "heidelberg", "stuttgart", "münchen", "muenchen", "berlin"]

if hasattr(st, "fragment"):
    fragment_dec = st.fragment
elif hasattr(st, "experimental_fragment"):
    fragment_dec = st.experimental_fragment
else:
    def fragment_dec(*args, **kwargs):
        def wrapper(f):
            return f
        return wrapper

# ---------------------------------------------------------
# HTTP-SESSION MIT RETRY & POOLING
# ---------------------------------------------------------
@st.cache_resource
def get_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(
        total=2,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# ---------------------------------------------------------
# CLIENT-SEITIGE LIVE-UHR (FÜR DIE SIDEBAR)
# ---------------------------------------------------------
def render_sidebar_clock():
    """Zeigt eine kontinuierlich im Browser laufende Uhrzeit im Sans-Serif-Font an."""
    clock_html = """
    <div style="font-family: -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; font-size: 0.875rem; color: #555; padding: 2px 0 6px 0;">
        🕒 Aktuelle Uhrzeit: <span id="live-clock" style="color: #555;">--:--:-- Uhr</span>
    </div>
    <script>
    (function() {
        function updateClock() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('de-DE', { 
                timeZone: 'Europe/Berlin', 
                hour12: false, 
                hour: '2-digit', 
                minute: '2-digit', 
                second: '2-digit' 
            });
            const el = document.getElementById('live-clock');
            if (el) el.innerText = timeStr + " Uhr";
        }
        updateClock();
        setInterval(updateClock, 1000);
    })();
    </script>
    """
    components.html(clock_html, height=35)

# ---------------------------------------------------------
# SANDBOXED AUDIO-ALARM
# ---------------------------------------------------------
def play_sound_cloud():
    js_code = """
    <!DOCTYPE html>
    <html>
    <head><title>Sound</title></head>
    <body style="margin:0; padding:0; overflow:hidden;">
    <script>
    (function() {
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            const ctx = new AudioContext();
            const strikeInterval = 0.55;
            for (let i = 0; i < 4; i++) {
                const strikeTime = ctx.currentTime + (i * strikeInterval);
                [800, 1150, 1900].forEach((freq, idx) => {
                    const osc = ctx.createOscillator();
                    const gain = ctx.createGain();
                    osc.type = idx === 0 ? 'sine' : 'triangle';
                    osc.frequency.setValueAtTime(freq, strikeTime);
                    const baseVol = idx === 0 ? 0.35 : 0.12;
                    gain.gain.setValueAtTime(baseVol, strikeTime);
                    gain.gain.exponentialRampToValueAtTime(0.0001, strikeTime + 0.42);
                    osc.connect(gain);
                    gain.connect(ctx.destination);
                    osc.start(strikeTime);
                    osc.stop(strikeTime + 0.45);
                });
            }
        } catch(e) {}
    })();
    </script>
    </body>
    </html>
    """
    components.html(js_code, height=0, width=0)

# ---------------------------------------------------------
# HILFSFUNKTIONEN & CACHED API-FETCH
# ---------------------------------------------------------
def clean_html(raw_html: str) -> str:
    """Entfernt führende Leerzeichen aus HTML-Zeilen, um die Markdown-Codeblock-Falle zu verhindern."""
    return "\n".join(line.strip() for line in raw_html.splitlines() if line.strip())

def is_fernverkehr(tr: dict) -> bool:
    raw = (tr.get("train") or tr.get("line") or "").strip().upper()
    return any(raw.startswith(prefix) for prefix in ["ICE", "TGV", "IC", "EC", "ECE"])

def clean_train_name(tr: dict) -> str:
    raw = tr.get("line") or tr.get("train") or ""
    raw = str(raw).strip()

    m_dup = re.match(r'^(?:RE|RB|S|MEX|SWE|HLB)\s+((?:S|RE|RB)\s*\d+.*)$', raw, re.IGNORECASE)
    if m_dup:
        return re.sub(r'^(S|RE|RB)\s+(\d+)', r'\1\2', m_dup.group(1).strip())

    m_simple = re.match(r'^(S|RE|RB)\s+(\d+.*)$', raw, re.IGNORECASE)
    if m_simple:
        return f"{m_simple.group(1).upper()}{m_simple.group(2).strip()}"

    return raw if raw else "Zug"

def is_neustadt_hbf(name: str) -> bool:
    if not name:
        return False
    n = name.lower()
    if "böbig" in n or "boebig" in n or "süd" in n or "sued" in n:
        return False
    return "neustadt" in n

def extract_station_names(route_list) -> List[str]:
    names = []
    for entry in route_list or []:
        if isinstance(entry, dict):
            names.append(str(entry.get("name", "") or ""))
        elif isinstance(entry, str):
            names.append(entry)
    return names

def parse_time(time_val, delay_min: int = 0, reference_dt: datetime.datetime = None) -> Optional[datetime.datetime]:
    if not time_val:
        return None
    match = re.search(r'(\d{1,2}):(\d{2})', str(time_val).strip())
    if not match:
        return None

    h, m = int(match.group(1)), int(match.group(2))
    ref = reference_dt or get_german_now()
    dt = ref.replace(hour=h, minute=m, second=0, microsecond=0)

    if delay_min:
        try:
            dt += datetime.timedelta(minutes=int(delay_min))
        except Exception:
            pass

    if dt < ref - datetime.timedelta(hours=6):
        dt += datetime.timedelta(days=1)
    elif dt > ref + datetime.timedelta(hours=18):
        dt -= datetime.timedelta(days=1)

    return dt

def int_or_default(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default

@st.cache_data(show_spinner=False)
def fetch_dbf_board_cached(station_name: str, mode: str, use_realtime: bool, time_bucket: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    session = get_http_session()
    headers = {"User-Agent": f"Streamlit-Steingleis-Monitor/{APP_VERSION} (github.com/heis2201/Steingleis)"}
    safe_station = urllib.parse.quote(station_name)
    url = f"https://dbf.finalrewind.org/{safe_station}.json?version=3"
    if mode == "arr":
        url += "&mode=arr"
    if not use_realtime:
        url += "&rt=0"

    try:
        resp = session.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]:
            return [], f"API-Fehler ({station_name}): {data['error']}"
        return data.get("departures", []), None
    except Exception as e:
        return [], f"Verbindungsfehler ({station_name}): {e}"

def fetch_dbf_board(station_name: str, mode: str = "dep", use_realtime: bool = True, is_neighbor: bool = False) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cfg = st.session_state.cfg
    cache_ttl = cfg.get("api_cache_through_sec", 120) if is_neighbor else cfg.get("api_cache_local_sec", 60)
    now_epoch = int(datetime.datetime.now().timestamp())
    time_bucket = now_epoch // max(cache_ttl, 5)
    return fetch_dbf_board_cached(station_name, mode, use_realtime, time_bucket)

def check_crosses_steingleis(tr: dict, mode: str = "dep") -> bool:
    """Prüft, ob eine Fahrt über den BÜ Steingleis (KBS 670 Pfälzerwald) führt."""
    dest = tr.get("destination") or tr.get("direction") or ""
    route_stops = extract_station_names(tr.get("route", []))

    if mode == "dep":
        post_stops = []
        found_neustadt = False
        for s in route_stops:
            s_lower = str(s).lower()
            if found_neustadt:
                post_stops.append(s)
            elif is_neustadt_hbf(s_lower):
                found_neustadt = True
        if not found_neustadt:
            post_stops = route_stops
        return any(kw in dest.lower() for kw in WEST_CORRIDOR_KEYWORDS) or                any(any(kw in str(s).lower() for kw in WEST_CORRIDOR_KEYWORDS) for s in post_stops)
    else:
        pre_stops = []
        for s in route_stops:
            s_lower = str(s).lower()
            if is_neustadt_hbf(s_lower):
                break
            pre_stops.append(s)
        origin = pre_stops[0] if pre_stops else tr.get("origin", "")
        return any(kw in origin.lower() for kw in WEST_CORRIDOR_KEYWORDS) or                any(any(kw in str(s).lower() for kw in WEST_CORRIDOR_KEYWORDS) for s in pre_stops)

def get_steingleis_events(use_realtime: bool, reference_dt: datetime.datetime) -> Tuple[list, list, list]:
    cfg = st.session_state.cfg
    lead_dep = cfg["lead_dep"]
    lag_dep = cfg["lag_dep"]
    lead_arr = cfg["lead_arr"]
    lag_arr = cfg["lag_arr"]
    track_through = cfg["track_through_trains"]
    transit_ma = cfg["transit_ma_min"]
    transit_kl = cfg["transit_kl_min"]

    raw_intervals = []
    zugliste = []
    seen_events = set()
    neustadt_train_numbers = set()
    api_errors = []

    dep_data, dep_err = fetch_dbf_board(STATION_NAME, mode="dep", use_realtime=use_realtime, is_neighbor=False)
    arr_data, arr_err = fetch_dbf_board(STATION_NAME, mode="arr", use_realtime=use_realtime, is_neighbor=False)

    if dep_err:
        api_errors.append(dep_err)
    if arr_err:
        api_errors.append(arr_err)

    # 1. Abfahrten ab Neustadt (nach Westen)
    for tr in dep_data:
        if tr.get("is_cancelled") or tr.get("isCancelled"):
            continue

        if check_crosses_steingleis(tr, mode="dep"):
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            dest = tr.get("destination") or tr.get("direction") or ""
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0

            num_match = re.search(r'\d+', train_name)
            if num_match:
                neustadt_train_numbers.add(num_match.group(0))

            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                event_id = f"DEP_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = dep_dt - datetime.timedelta(seconds=lead_dep)
                    end = dep_dt + datetime.timedelta(seconds=lag_dep)

                    info = f"Abfahrt: {train_name} -> {dest}"
                    raw_intervals.append((start, end, info))
                    delay_str = f" (+{delay})" if (use_realtime and delay > 0) else ""
                    zugliste.append({
                        "Richtung": "➡️ Abfahrt (nach Westen)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": dep_dt.strftime("%H:%M") + delay_str,
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

    # 2. Ankünfte in Neustadt (aus Westen)
    kombinierte_ankuenfte = arr_data + dep_data
    for tr in kombinierte_ankuenfte:
        if tr.get("is_cancelled") or tr.get("isCancelled"):
            continue

        if check_crosses_steingleis(tr, mode="arr"):
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time") or tr.get("scheduledTime")
            delay = int_or_default(tr.get("delayArrival", tr.get("delay", 0)), 0) if use_realtime else 0

            num_match = re.search(r'\d+', train_name)
            if num_match:
                neustadt_train_numbers.add(num_match.group(0))

            route_stops = extract_station_names(tr.get("route", []))
            pre_stops = []
            for s in route_stops:
                s_lower = str(s).lower()
                if is_neustadt_hbf(s_lower):
                    break
                pre_stops.append(s)
            origin = pre_stops[0] if pre_stops else tr.get("origin", "Pfälzerwald")

            arr_dt = parse_time(sched_time, delay, reference_dt)
            if arr_dt:
                event_id = f"ARR_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = arr_dt - datetime.timedelta(seconds=lead_arr)
                    end = arr_dt - datetime.timedelta(seconds=lag_arr)

                    info = f"Ankunft: {train_name} aus {origin}"
                    raw_intervals.append((start, end, info))
                    delay_str = f" (+{delay})" if (use_realtime and delay > 0) else ""
                    zugliste.append({
                        "Richtung": "⬅️ Ankunft (aus Westen)",
                        "Zug": train_name,
                        "Start/Ziel": origin,
                        "Fahrplanzeit": arr_dt.strftime("%H:%M") + delay_str,
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

    # 3. Durchfahrten (ICE/TGV)
    if track_through:
        ma_dep_data, ma_err = fetch_dbf_board("Mannheim Hbf", mode="dep", use_realtime=use_realtime, is_neighbor=True)
        kl_dep_data, kl_err = fetch_dbf_board("Kaiserslautern Hbf", mode="dep", use_realtime=use_realtime, is_neighbor=True)
        if ma_err:
            api_errors.append(ma_err)
        if kl_err:
            api_errors.append(kl_err)

        for tr in ma_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"):
                continue

            dest = tr.get("destination") or tr.get("direction") or ""
            route_stops = extract_station_names(tr.get("route", []))

            matches_west = any(kw in dest.lower() for kw in WEST_BOUND_DESTS) or                            any(any(kw in str(s).lower() for kw in WEST_BOUND_DESTS) for s in route_stops)
            if not matches_west or any(is_neustadt_hbf(str(s)) for s in route_stops) or "neustadt" in dest.lower():
                continue

            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers:
                continue

            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0

            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                pass_dt = dep_dt + datetime.timedelta(minutes=transit_ma)
                event_id = f"THROUGH_WEST_{train_name}_{sched_time}"

                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = pass_dt - datetime.timedelta(seconds=lead_dep)
                    end = pass_dt + datetime.timedelta(seconds=lag_dep)

                    info = f"Durchfahrt: {train_name} -> {dest}"
                    raw_intervals.append((start, end, info))
                    delay_str = f" (+{delay})" if (use_realtime and delay > 0) else ""
                    zugliste.append({
                        "Richtung": "➡️ Durchfahrt (nach Westen)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')}{delay_str}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

        for tr in kl_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"):
                continue

            dest = tr.get("destination") or tr.get("direction") or ""
            route_stops = extract_station_names(tr.get("route", []))

            matches_east = any(kw in dest.lower() for kw in EAST_BOUND_DESTS) or                            any(any(kw in str(s).lower() for kw in EAST_BOUND_DESTS) for s in route_stops)
            if not matches_east or any(is_neustadt_hbf(str(s)) for s in route_stops) or "neustadt" in dest.lower():
                continue

            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers:
                continue

            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0

            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                pass_dt = dep_dt + datetime.timedelta(minutes=transit_kl)
                event_id = f"THROUGH_EAST_{train_name}_{sched_time}"

                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = pass_dt - datetime.timedelta(seconds=lead_arr)
                    end = pass_dt + datetime.timedelta(seconds=lag_arr)

                    info = f"Durchfahrt: {train_name} -> {dest}"
                    raw_intervals.append((start, end, info))
                    delay_str = f" (+{delay})" if (use_realtime and delay > 0) else ""
                    zugliste.append({
                        "Richtung": "⬅️ Durchfahrt (aus Westen)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')}{delay_str}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

    zugliste = sorted(zugliste, key=lambda x: x["_start_dt"])
    return raw_intervals, zugliste, api_errors

def merge_intervals(intervals, min_gap_sec: int):
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = []
    curr_start, curr_end, curr_desc = sorted_intervals[0]
    curr_trains = [curr_desc]

    for next_start, next_end, next_desc in sorted_intervals[1:]:
        if next_start <= curr_end + datetime.timedelta(seconds=min_gap_sec):
            curr_end = max(curr_end, next_end)
            curr_trains.append(next_desc)
        else:
            merged.append((curr_start, curr_end, curr_trains))
            curr_start, curr_end = next_start, next_end
            curr_trains = [next_desc]

    merged.append((curr_start, curr_end, curr_trains))
    return merged

def build_timeline_dataframe(base_dt: datetime.datetime, merged_intervals: list, duration_min: int = 60) -> pd.DataFrame:
    window_start = base_dt
    window_end = base_dt + datetime.timedelta(minutes=duration_min)
    events = []
    curr = window_start

    sorted_int = sorted(merged_intervals, key=lambda x: x[0])

    for start, end, trains in sorted_int:
        if end <= window_start or start >= window_end:
            continue

        eff_start = max(window_start, start)
        eff_end = min(window_end, end)

        if eff_start > curr:
            events.append({
                "Start": curr,
                "Ende": eff_start,
                "Status": "Freie Fahrt (Offen)",
                "Details": "BÜ geöffnet"
            })

        events.append({
            "Start": eff_start,
            "Ende": eff_end,
            "Status": "Schranke Geschlossen",
            "Details": " + ".join(trains)
        })
        curr = max(curr, eff_end)

    if curr < window_end:
        events.append({
            "Start": curr,
            "Ende": window_end,
            "Status": "Freie Fahrt (Offen)",
            "Details": "BÜ geöffnet"
        })

    return pd.DataFrame(events)

# ---------------------------------------------------------
# BAHNHOFSTAFEL HELPER IM AUTHENTISCHEN DB-LOOK (FIA)
# ---------------------------------------------------------
def render_db_board_card(tr: dict, mode: str = "dep", use_realtime: bool = True):
    """Rendert einen Zug als authentische DB-Fahrgastinformations-Kachel (dunkelblau)."""
    is_cancelled = bool(tr.get("is_cancelled") or tr.get("isCancelled"))
    train_name = clean_train_name(tr)
    
    if mode == "dep":
        sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime") or "--:--"
        delay_raw = tr.get("delayDeparture")
    else:
        sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time") or tr.get("scheduledTime") or "--:--"
        delay_raw = tr.get("delayArrival")
        
    if delay_raw is None:
        delay_raw = tr.get("delay", 0)
    delay = int_or_default(delay_raw, 0) if use_realtime else 0
    platform = tr.get("platform") or tr.get("scheduledPlatform") or "-"

    # Station und Zwischenhalte
    route_stops = extract_station_names(tr.get("route", []))
    if mode == "dep":
        main_station = tr.get("destination") or tr.get("direction") or ""
        if not main_station and route_stops:
            main_station = route_stops[-1]
        if not main_station:
            main_station = "Neustadt(Weinstr)Hbf"

        post_stops = []
        found_neustadt = False
        for s in route_stops:
            if found_neustadt:
                post_stops.append(str(s))
            elif is_neustadt_hbf(str(s)):
                found_neustadt = True
        if not found_neustadt:
            post_stops = [str(s) for s in route_stops]
        via_stops = " • ".join(post_stops[:5]) if post_stops else ""
        via_label = f"über: {via_stops}" if via_stops else ""
    else:
        pre_stops = []
        for s in route_stops:
            if is_neustadt_hbf(str(s)):
                break
            pre_stops.append(str(s))
        origin = pre_stops[0] if pre_stops else tr.get("origin", "")
        if not origin:
            origin = "Unbekannt"
        main_station = origin
        via_stops = " • ".join(pre_stops[1:6]) if len(pre_stops) > 1 else ""
        via_label = f"aus Richtung: {via_stops}" if via_stops else ""

    # BÜ Steingleis Relevanz
    crosses_bu = check_crosses_steingleis(tr, mode=mode)

    # Zeit- und Verspätungsspalte
    if is_cancelled:
        time_html = f'<div style="font-size: 1.25rem; font-weight: 700; color: #ff5252; text-decoration: line-through; line-height: 1.1;">{sched_time}</div><div style="font-size: 0.75rem; font-weight: 700; color: #ff5252; text-transform: uppercase; margin-top: 3px;">Fällt aus</div>'
    elif use_realtime and delay > 0:
        time_html = f'<div style="font-size: 1.25rem; font-weight: 700; color: #ffffff; line-height: 1.1;">{sched_time}</div><div style="font-size: 0.82rem; font-weight: 700; color: #ffb300; margin-top: 3px;">+{delay} min</div>'
    elif use_realtime:
        time_html = f'<div style="font-size: 1.25rem; font-weight: 700; color: #ffffff; line-height: 1.1;">{sched_time}</div><div style="font-size: 0.75rem; color: #4caf50; font-weight: 600; margin-top: 3px;">Pünktlich</div>'
    else:
        time_html = f'<div style="font-size: 1.25rem; font-weight: 700; color: #ffffff; line-height: 1.1;">{sched_time}</div><div style="font-size: 0.75rem; color: #90caf9; margin-top: 3px;">Soll-Plan</div>'

    # Badge BÜ Steingleis
    bu_badge = '<span style="background-color: #b71c1c; color: #ffffff; padding: 2px 7px; border-radius: 3px; font-size: 0.75rem; font-weight: 700; margin-left: 8px; letter-spacing: 0.3px;">🚦 BÜ Steingleis</span>' if crosses_bu else ''

    # Zuggattungs-Badge Styling (ICE weiß/grau, Nahverkehr blau)
    is_fv = is_fernverkehr(tr)
    badge_bg = "#ffffff" if is_fv else "#1565c0"
    badge_fg = "#0c1e38" if is_fv else "#ffffff"

    # Meldungen extrahieren
    messages = tr.get("messages", {})
    msg_list = []
    if isinstance(messages, dict):
        for m in messages.values():
            if isinstance(m, list):
                for item in m:
                    if isinstance(item, dict) and "text" in item:
                        msg_list.append(item["text"])
                    elif isinstance(item, str):
                        msg_list.append(item)
    elif isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict) and "text" in item:
                msg_list.append(item["text"])
            elif isinstance(item, str):
                msg_list.append(item)
    msg_str = " | ".join(msg_list) if msg_list else ""
    msg_html = f'<div style="color: #ffd54f; font-size: 0.78rem; font-weight: 600; margin-top: 4px;">⚠️ {msg_str}</div>' if msg_str else ''
    via_html = f'<div style="font-size: 0.8rem; color: #9bb2d4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{via_label}</div>' if via_label else ''

    card_html = (
        f'<div style="background-color: #0b1a30; border-bottom: 2px solid #1c365d; border-radius: 4px; padding: 10px 14px; margin-bottom: 7px; font-family: -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; display: flex; align-items: center; justify-content: space-between; box-shadow: 0 1px 3px rgba(0,0,0,0.25);">'
        f'<div style="min-width: 78px; text-align: left;">{time_html}</div>'
        f'<div style="flex-grow: 1; padding: 0 16px; overflow: hidden;">'
        f'<div style="display: flex; align-items: center; flex-wrap: wrap; margin-bottom: 3px;">'
        f'<span style="background-color: {badge_bg}; color: {badge_fg}; font-size: 0.85rem; font-weight: 700; padding: 2px 7px; border-radius: 3px; margin-right: 10px;">{train_name}</span>'
        f'<span style="font-size: 1.15rem; font-weight: 700; color: #ffffff; letter-spacing: 0.2px;">{main_station}</span>'
        f'{bu_badge}'
        f'</div>'
        f'{via_html}'
        f'{msg_html}'
        f'</div>'
        f'<div style="min-width: 68px; text-align: center; border: 2px solid #2a528a; background-color: #061122; padding: 4px 8px; border-radius: 4px;">'
        f'<div style="font-size: 0.65rem; color: #9bb2d4; text-transform: uppercase; font-weight: 700; letter-spacing: 0.5px;">Gleis</div>'
        f'<div style="font-size: 1.3rem; font-weight: 800; color: #ffffff; line-height: 1.1;">{platform}</div>'
        f'</div>'
        f'</div>'
    )
    st.markdown(clean_html(card_html), unsafe_allow_html=True)

# ---------------------------------------------------------
# SEITEN-NAVIGATION (SIDEBAR)
# ---------------------------------------------------------
with st.sidebar:
    st.title("🚦 BÜ Steingleis")
    st.caption(f"App-Version {APP_VERSION}")
    
    active_page = st.radio("Bereich wählen:", ["🚦 Monitor", "🚉 Bahnhofstafel", "⚙️ Einstellungen"], index=0)
    st.markdown("---")

    if active_page in ["🚦 Monitor", "🚉 Bahnhofstafel"]:
        st.subheader("Modus & Zeit")
        modus = st.radio("Betriebsmodus:", ["🔴 Live-Echtzeit", "📅 Fahrplan-Analyse (Wunschzeit)"])
        now_german = get_german_now()

        if modus == "🔴 Live-Echtzeit":
            use_realtime = True
            ui_interval = int(st.session_state.cfg.get("ui_refresh_sec", 5))
            auto_refresh = st.toggle(
                f"🔄 Auto-Refresh ({ui_interval}s)", 
                value=True
            )
            plan_selected_dt = None
        else:
            use_realtime = False
            auto_refresh = False
            st.caption("Echtzeit-Verspätungen deaktiviert (reiner Soll-Fahrplan).")
            col_d, col_t = st.columns(2)
            sel_date = col_d.date_input("Datum:", value=now_german.date())
            sel_time = col_t.time_input("Uhrzeit:", value=now_german.time())
            plan_selected_dt = datetime.datetime.combine(sel_date, sel_time)

        # Kontinuierlich laufende Live-Uhr direkt in der Sidebar
        render_sidebar_clock()

        if st.button("🔄 Jetzt Daten neu laden", width="stretch"):
            st.cache_data.clear()
            st.rerun()

    st.markdown('<div style="flex-grow: 1;"></div>', unsafe_allow_html=True)
    with st.container():
        st.markdown("---")
        st.markdown(
            """
            <div style="font-size: 0.85em; color: #888; line-height: 1.6;">
                <a href="https://github.com/heis2201/Steingleis/blob/main/README.md" target="_blank" style="color: #666; text-decoration: none;">📄 Dokumentation (README)</a><br>
                <a href="https://github.com/heis2201/Steingleis/blob/main/LICENSE" target="_blank" style="color: #666; text-decoration: none;">⚖️ MIT Lizenz</a><br>
                <br>
                <span style="font-style: italic; font-size: 0.9em; color: #aaa;">
                    Prognosen ohne Gewähr.<br>
                    Güterzüge nicht erfasst.
                </span>
            </div>
            """,
            unsafe_allow_html=True
        )

# =========================================================
# SEITE 1: MONITOR
# =========================================================
if active_page == "🚦 Monitor":
    refresh_interval = int(st.session_state.cfg.get("ui_refresh_sec", 5)) if (use_realtime and auto_refresh) else None

    @fragment_dec(run_every=refresh_interval)
    def render_live_dashboard():
        current_time_tick = get_german_now()
        calc_base_dt = current_time_tick if use_realtime else plan_selected_dt

        raw_intervals, zugliste, api_errors = get_steingleis_events(use_realtime, calc_base_dt)
        min_gap = int(st.session_state.cfg.get("min_gap", 30))
        merged = merge_intervals(raw_intervals, min_gap)

        st.title("🚦 BÜ Steingleis (Neustadt/Weinstraße)")
        if not use_realtime:
            st.caption(f"🕒 Gewählter Analysezeitpunkt: **{calc_base_dt.strftime('%d.%m.%Y um %H:%M')} Uhr**")

        if api_errors:
            with st.expander("⚠️ Verbindungswarnung zur Fahrplan-API", expanded=True):
                for err in api_errors:
                    st.warning(err)
                st.caption("Aufgrund von Verbindungsstörungen können Schließungen fehlen.")

        is_closed = False
        current_start = None
        current_end = None
        next_closure = None
        active_reasons = []

        for start, end, trains in merged:
            if start <= calc_base_dt <= end:
                is_closed = True
                current_start = start
                current_end = end
                active_reasons = trains
                break
            elif start > calc_base_dt:
                if next_closure is None or start < next_closure[0]:
                    next_closure = (start, end, trains)

        # Akustischer Alarm (Audit-Fix 3: Entprellt - nur 1x pro Schließung)
        if use_realtime and st.session_state.cfg.get("sound_alarm", True):
            sound_warn_sec = int(st.session_state.cfg.get("sound_warn_sec", 45))
            if next_closure:
                s_next = next_closure[0]
                closure_id = s_next.strftime("%Y-%m-%d %H:%M:%S")
                sec_to_next = (s_next - calc_base_dt).total_seconds()
                if 0 < sec_to_next <= sound_warn_sec and st.session_state.last_alarmed_closure != closure_id:
                    st.session_state.last_alarmed_closure = closure_id
                    play_sound_cloud()
            elif is_closed and current_start:
                closure_id = current_start.strftime("%Y-%m-%d %H:%M:%S")
                sec_since_start = (calc_base_dt - current_start).total_seconds()
                if 0 <= sec_since_start <= 15 and st.session_state.last_alarmed_closure != closure_id:
                    st.session_state.last_alarmed_closure = closure_id
                    play_sound_cloud()
            elif not next_closure and not is_closed:
                st.session_state.last_alarmed_closure = None

        col1, col2 = st.columns([1, 1])

        with col1:
            sub_title = "Live-Status" if use_realtime else f"Status am {calc_base_dt.strftime('%d.%m. um %H:%M')} Uhr (Plan)"
            st.subheader(sub_title)
            
            if not zugliste and api_errors:
                st.warning("### ⚠️ STATUS UNBEKANNT (KEINE DATEN)")
                st.metric("Schrankenstatus", "Unbekannt", "API-Fehler")
                st.caption("Die DB-Schnittstelle liefert keine Daten. Eine Freigabe kann nicht garantiert werden.")
            elif is_closed:
                st.error("### 🛑 SCHRANKE GESCHLOSSEN")
                rest_sec = int((current_end - calc_base_dt).total_seconds())
                st.metric("Geschlossen bis ca.", 
                          current_end.strftime("%H:%M:%S"), 
                          f"Noch {rest_sec // 60}m {rest_sec % 60:02d}s")
                st.caption("Auslöser:")
                for r in active_reasons:
                    st.write(f"- {r}")
            else:
                st.success("### ✅ SCHRANKE OFFEN (FREIE FAHRT)")
                if next_closure:
                    s_next, e_next, tr_next = next_closure
                    diff_sec = int((s_next - calc_base_dt).total_seconds())
                    st.metric("Nächste Schließung um", 
                              s_next.strftime("%H:%M:%S"), 
                              f"In ca. {diff_sec // 60}m {diff_sec % 60:02d}s")
                    st.caption(f"Nächster Zug: {tr_next[0]}")
                else:
                    st.info("Im analysierten Zeitfenster stehen keine Schließungen an.")

        with col2:
            st.subheader("📋 Prognostizierte Schließphasen")
            future_rows = []
            for s, e, trs in merged:
                if e >= calc_base_dt:
                    dur = int((e - s).total_seconds())
                    future_rows.append({
                        "Von": s.strftime("%H:%M:%S"),
                        "Bis": e.strftime("%H:%M:%S"),
                        "Dauer": f"{dur // 60}m {dur % 60:02d}s",
                        "Betroffene Züge": " + ".join(trs)
                    })
            if future_rows:
                st.dataframe(pd.DataFrame(future_rows), width="stretch", hide_index=True)
            else:
                st.write("Keine Schließungen im gewählten Zeitabschnitt.")

        # 60-Minuten-Zeitstrahl
        st.markdown("---")
        st.subheader("⏱️ Zeitstrahl nächste 60 Minuten")
        df_timeline = build_timeline_dataframe(calc_base_dt, merged, duration_min=60)
        
        if not df_timeline.empty:
            window_end = calc_base_dt + datetime.timedelta(minutes=60)
            chart = (
                alt.Chart(df_timeline)
                .mark_bar(cornerRadius=4, height=36)
                .encode(
                    x=alt.X(
                        'Start:T',
                        title='Uhrzeit (5-Minuten-Takt)',
                        scale=alt.Scale(domain=[calc_base_dt, window_end]),
                        axis=alt.Axis(
                            format='%H:%M',
                            tickCount=12,
                            labelAngle=0,
                            labelFontSize=11,
                            grid=True,
                            gridDash=[2, 2],
                            orient='bottom'
                        )
                    ),
                    x2='Ende:T',
                    color=alt.Color(
                        'Status:N',
                        scale=alt.Scale(
                            domain=['Freie Fahrt (Offen)', 'Schranke Geschlossen'],
                            range=['#22c55e', '#ef4444']
                        ),
                        legend=alt.Legend(title=None, orient='top', labelFontSize=12)
                    ),
                    tooltip=[
                        alt.Tooltip('Status:N', title='Status'),
                        alt.Tooltip('Start:T', format='%H:%M:%S', title='Von'),
                        alt.Tooltip('Ende:T', format='%H:%M:%S', title='Bis'),
                        alt.Tooltip('Details:N', title='Details / Züge')
                    ]
                )
                .properties(height=125)
            )
            st.altair_chart(chart, width="stretch")

        with st.expander(f"🔍 Alle erfassten Züge am Steingleis ({len(zugliste)} Fahrten)"):
            if zugliste:
                df_display = pd.DataFrame(zugliste).drop(columns=["_start_dt"])
                st.dataframe(df_display, width="stretch", hide_index=True)
            else:
                st.write("Keine Fahrten gefunden.")

    render_live_dashboard()

# =========================================================
# SEITE 2: BAHNHOFSTAFEL NEUSTADT (WEINSTR) HBF (DB-LOOK)
# =========================================================
elif active_page == "🚉 Bahnhofstafel":
    refresh_interval = int(st.session_state.cfg.get("ui_refresh_sec", 5)) if (use_realtime and auto_refresh) else None

    @fragment_dec(run_every=refresh_interval)
    def render_station_board():
        st.title("🚉 Bahnhofstafel Neustadt (Weinstr) Hbf")
        mode_label = "🔴 Live-Echtzeit" if use_realtime else f"📅 Plan-Stand ({plan_selected_dt.strftime('%d.%m.%Y %H:%M')} Uhr)"
        st.caption(f"Digitale Fahrgastinformation via DBF • {mode_label}")

        dep_data, dep_err = fetch_dbf_board(STATION_NAME, mode="dep", use_realtime=use_realtime, is_neighbor=False)
        arr_data, arr_err = fetch_dbf_board(STATION_NAME, mode="arr", use_realtime=use_realtime, is_neighbor=False)

        if dep_err or arr_err:
            with st.expander("⚠️ Verbindungsstatus zur API", expanded=True):
                if dep_err:
                    st.warning(dep_err)
                if arr_err:
                    st.warning(arr_err)

        tab_dep, tab_arr = st.tabs(["➡️ Abfahrten", "⬅️ Ankünfte"])

        with tab_dep:
            col_f1, col_f2 = st.columns([1, 2])
            with col_f1:
                filter_steingleis_dep = st.checkbox("Nur Züge über BÜ Steingleis", value=False, key="filter_dep_steingleis")
            with col_f2:
                search_dep = st.text_input("🔍 Filter (Zug, Ziel):", "", key="search_dep")

            filtered_dep = []
            for tr in dep_data:
                dest = tr.get("destination") or tr.get("direction") or ""
                train_name = clean_train_name(tr)
                if filter_steingleis_dep and not check_crosses_steingleis(tr, mode="dep"):
                    continue
                if search_dep:
                    s_low = search_dep.strip().lower()
                    if s_low not in dest.lower() and s_low not in train_name.lower():
                        continue
                filtered_dep.append(tr)

            if filtered_dep:
                # Tabellenkopf im DB-Monitor-Stil
                header_html = (
                    '<div style="background-color: #061122; border-bottom: 2px solid #2a528a; padding: 6px 14px; margin-bottom: 8px; border-radius: 4px; display: flex; justify-content: space-between; font-size: 0.75rem; font-weight: 700; color: #9bb2d4; text-transform: uppercase; letter-spacing: 0.5px;">'
                    '<div style="min-width: 78px;">Zeit</div>'
                    '<div style="flex-grow: 1; padding: 0 16px;">Zug / Fahrtziel</div>'
                    '<div style="min-width: 68px; text-align: center;">Gleis</div>'
                    '</div>'
                )
                st.markdown(clean_html(header_html), unsafe_allow_html=True)
                for tr in filtered_dep:
                    render_db_board_card(tr, mode="dep", use_realtime=use_realtime)
            else:
                st.info("Keine Abfahrten für die aktuellen Filterkriterien gefunden.")

        with tab_arr:
            col_fa1, col_fa2 = st.columns([1, 2])
            with col_fa1:
                filter_steingleis_arr = st.checkbox("Nur Züge über BÜ Steingleis", value=False, key="filter_arr_steingleis")
            with col_fa2:
                search_arr = st.text_input("🔍 Filter (Zug, Herkunft):", "", key="search_arr")

            filtered_arr = []
            for tr in arr_data:
                route_stops = extract_station_names(tr.get("route", []))
                pre_stops = []
                for s in route_stops:
                    if is_neustadt_hbf(str(s)):
                        break
                    pre_stops.append(s)
                origin = pre_stops[0] if pre_stops else tr.get("origin", "")
                train_name = clean_train_name(tr)
                if filter_steingleis_arr and not check_crosses_steingleis(tr, mode="arr"):
                    continue
                if search_arr:
                    s_low = search_arr.strip().lower()
                    if s_low not in origin.lower() and s_low not in train_name.lower():
                        continue
                filtered_arr.append(tr)

            if filtered_arr:
                # Tabellenkopf im DB-Monitor-Stil
                header_html = (
                    '<div style="background-color: #061122; border-bottom: 2px solid #2a528a; padding: 6px 14px; margin-bottom: 8px; border-radius: 4px; display: flex; justify-content: space-between; font-size: 0.75rem; font-weight: 700; color: #9bb2d4; text-transform: uppercase; letter-spacing: 0.5px;">'
                    '<div style="min-width: 78px;">Zeit</div>'
                    '<div style="flex-grow: 1; padding: 0 16px;">Zug / Herkunft</div>'
                    '<div style="min-width: 68px; text-align: center;">Gleis</div>'
                    '</div>'
                )
                st.markdown(clean_html(header_html), unsafe_allow_html=True)
                for tr in filtered_arr:
                    render_db_board_card(tr, mode="arr", use_realtime=use_realtime)
            else:
                st.info("Keine Ankünfte für die aktuellen Filterkriterien gefunden.")

    render_station_board()

# =========================================================
# SEITE 3: EINSTELLUNGEN
# =========================================================
elif active_page == "⚙️ Einstellungen":
    st.title("⚙️ Einstellungen & Parameter")
    st.caption(f"Version {APP_VERSION} • Konfiguration des Stellwerksmodells, der Taktung und der API-Caches.")

    if st.session_state.get("config_load_error"):
        st.warning("⚠️ Die gespeicherte `config.json` war fehlerhaft/unlesbar und wurde temporär durch Standardwerte ersetzt.")

    cfg = st.session_state.cfg
    tab1, tab2, tab3 = st.tabs(["🛠️ Stellwerks-Parameter", "🚄 Fernverkehr & Durchfahrten", "⏱️ Taktung, API & Audio"])

    with tab1:
        st.subheader("Vor- und Nachlaufzeiten am BÜ Steingleis")
        c1, c2 = st.columns(2)
        with c1:
            cfg["lead_dep"] = st.slider(
                "Schließung vor Abfahrt Hbf (Sek.)", 30, 360, int(cfg.get("lead_dep", 240)), step=10,
                help="Vorlaufzeit vor Planabfahrt am Hbf inklusive Signal- und Fahrstraßenstellung."
            )
            cfg["lag_dep"] = st.slider(
                "Öffnung nach Abfahrt Hbf (Sek.)", 30, 180, int(cfg.get("lag_dep", 90)), step=10,
                help="Zeit bis der abfahrende Zug den BÜ komplett geräumt hat."
            )
        with c2:
            cfg["lead_arr"] = st.slider(
                "Schließung vor Ankunft Hbf (Sek.)", 60, 240, int(cfg.get("lead_arr", 160)), step=10,
                help="Zeitspanne vor Planankunft am Bahnsteig, wenn die Schranke fällt."
            )
            cfg["lag_arr"] = st.slider(
                "Öffnung vor Ankunft Hbf (Sek.)", 10, 90, int(cfg.get("lag_arr", 40)), step=5,
                help="Zeit vor Ankunft am Bahnsteig, zu der die Zugspitze den BÜ bereits passiert hat."
            )

        cfg["min_gap"] = st.slider(
            "Mindestöffnungslücke zwischen zwei Zügen (Sek.)", 0, 120, int(cfg.get("min_gap", 30)), step=10,
            help="Ist die Zeit zwischen Öffnung und nächster Schließung kürzer als dieser Wert, bleibt die Schranke geschlossen."
        )

    with tab2:
        st.subheader("Fernverkehrs-Laufzeitüberwachung (KBS 670)")
        cfg["track_through_trains"] = st.toggle(
            "ICE- und TGV-Durchfahrten ohne Halt in Neustadt erfassen",
            value=parse_bool_value(cfg.get("track_through_trains", True), True),
            help="Überwacht die Nachbarknoten Mannheim Hbf und Kaiserslautern Hbf auf Züge Richtung Paris / Frankfurt."
        )

        col_ma, col_kl = st.columns(2)
        with col_ma:
            cfg["transit_ma_min"] = st.slider(
                "Fahrzeit Mannheim Hbf -> BÜ Steingleis (Min.)", 10, 25, int(cfg.get("transit_ma_min", 16)), step=1
            )
        with col_kl:
            cfg["transit_kl_min"] = st.slider(
                "Fahrzeit Kaiserslautern Hbf -> BÜ Steingleis (Min.)", 12, 30, int(cfg.get("transit_kl_min", 19)), step=1
            )

    with tab3:
        st.subheader("⏱️ Taktung & API-Schonung")
        st.caption("Entkopplung von Benutzeroberfläche und externen Netzwerkaufrufen zum Schutz der DBF-API.")

        c_ui, c_api1, c_api2 = st.columns(3)
        with c_ui:
            cfg["ui_refresh_sec"] = st.slider(
                "🖥️ UI-Neuberechnung (Sek.)", 2, 30, int(cfg.get("ui_refresh_sec", 5)), step=1,
                help="Intervall, in dem die Schranken-Intervalle und Grafiken im Browser lokal durchgerechnet werden."
            )
        with c_api1:
            cfg["api_cache_local_sec"] = st.slider(
                "📡 DB-Abruf Neustadt (Sek.)", 10, 120, int(cfg.get("api_cache_local_sec", 60)), step=5,
                help="Cache-Gültigkeit für Ankünfte und Abfahrten in Neustadt(Weinstr)Hbf."
            )
        with c_api2:
            cfg["api_cache_through_sec"] = st.slider(
                "🚄 DB-Abruf Fernverkehr (Sek.)", 15, 180, int(cfg.get("api_cache_through_sec", 120)), step=15,
                help="Cache-Gültigkeit für ICE/TGV-Abfragen der Nachbarknoten Mannheim und Kaiserslautern."
            )

        st.markdown("---")
        st.subheader("🔔 Akustische Warnung")
        col_snd1, col_snd2 = st.columns(2)
        with col_snd1:
            cfg["sound_alarm"] = st.toggle(
                "Schrankengong aktivieren",
                value=parse_bool_value(cfg.get("sound_alarm", True), True)
            )
            cfg["sound_warn_sec"] = st.slider(
                "Vorwarnzeit vor Schließung (Sek.)", 15, 120, int(cfg.get("sound_warn_sec", 45)), step=5
            )
        with col_snd2:
            if st.button("🔊 Signalton jetzt testen"):
                play_sound_cloud()

    st.markdown("---")
    col_save, col_reset = st.columns([1, 1])
    with col_save:
        if st.button("💾 Einstellungen als neuen Standard in config.json speichern", type="primary", width="stretch"):
            success = save_config_atomic(cfg)
            if success:
                st.session_state["config_load_error"] = False
                st.success("✅ Einstellungen validiert und dauerhaft in config.json gespeichert!")
            else:
                st.error("❌ Fehler beim atomaren Speichern der Konfiguration.")

    with col_reset:
        if st.button("↩️ Auf Werkseinstellungen zurücksetzen", width="stretch"):
            st.session_state.cfg = DEFAULT_CONFIG.copy()
            save_config_atomic(DEFAULT_CONFIG)
            st.session_state["config_load_error"] = False
            st.info("Werkseinstellungen wiederhergestellt.")
            st.rerun()
