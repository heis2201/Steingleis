import datetime
import json
import os
import re
import tempfile
import urllib.parse
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Any, Optional
import hashlib
from dataclasses import dataclass, asdict, field

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
APP_VERSION = "0.3.0"

st.set_page_config(
    page_title=f"BÜ Steingleis Monitor v{APP_VERSION}",
    page_icon="🚦",
    layout="wide"
)

BERLIN_TZ = ZoneInfo("Europe/Berlin")

def get_german_now() -> datetime.datetime:
    return datetime.datetime.now(BERLIN_TZ).replace(tzinfo=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
STATION_NAME = "Neustadt(Weinstr)Hbf"

# =========================================================
# ADMIN-AUTHENTIFIZIERUNG VIA SHA-256 HASH
# =========================================================
ADMIN_PWD_HASH = "825c6e3ce5143287592b8c50dc57b860808ae393f361eb37e7a4dfa8d4ff4695"
SALT = "steingleis_kbs670"

def verify_admin_password(input_password: str) -> bool:
    if not input_password:
        return False
    calc_hash = hashlib.sha256((input_password.strip() + SALT).encode('utf-8')).hexdigest()
    return calc_hash == ADMIN_PWD_HASH

# =========================================================
# TYPISIERTE MODELLE (DATACLASSES)
# =========================================================
def parse_bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool): return value
    if isinstance(value, int) and value in (0, 1): return bool(value)
    if isinstance(value, str):
        norm = value.strip().lower()
        if norm in {"1", "true", "yes", "y", "on", "ja"}: return True
        if norm in {"0", "false", "no", "n", "off", "nein", ""}: return False
    return bool(default)

def int_or_default(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "": return default
        return int(value)
    except: return default

@dataclass
class AppConfig:
    ui_refresh_sec: int = 5
    api_cache_local_sec: int = 60
    api_cache_through_sec: int = 120
    lead_dep: int = 240
    lag_dep: int = 90
    lead_arr: int = 160
    lag_arr: int = 40
    min_gap: int = 30
    track_through_trains: bool = True
    transit_ma_min: int = 16
    transit_kl_min: int = 19
    sound_alarm: bool = True
    sound_warn_sec: int = 45

    @classmethod
    def from_dict(cls, data: dict):
        if not isinstance(data, dict): data = {}
        return cls(
            ui_refresh_sec=int_or_default(data.get("ui_refresh_sec"), 5),
            api_cache_local_sec=int_or_default(data.get("api_cache_local_sec"), 60),
            api_cache_through_sec=int_or_default(data.get("api_cache_through_sec"), 120),
            lead_dep=int_or_default(data.get("lead_dep"), 240),
            lag_dep=int_or_default(data.get("lag_dep"), 90),
            lead_arr=int_or_default(data.get("lead_arr"), 160),
            lag_arr=int_or_default(data.get("lag_arr"), 40),
            min_gap=int_or_default(data.get("min_gap"), 30),
            track_through_trains=parse_bool_value(data.get("track_through_trains"), True),
            transit_ma_min=int_or_default(data.get("transit_ma_min"), 16),
            transit_kl_min=int_or_default(data.get("transit_kl_min"), 19),
            sound_alarm=parse_bool_value(data.get("sound_alarm"), True),
            sound_warn_sec=int_or_default(data.get("sound_warn_sec"), 45)
        )

    def to_dict(self):
        return asdict(self)

@dataclass
class ClosureEvent:
    start: datetime.datetime
    end: datetime.datetime
    trains: List[str] = field(default_factory=list)

def load_config() -> AppConfig:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return AppConfig.from_dict(data)
        except Exception:
            st.session_state["config_load_error"] = True
            return AppConfig()
    return AppConfig()

def save_config_atomic(cfg: AppConfig, filepath: str = CONFIG_FILE) -> bool:
    try:
        dir_name = os.path.dirname(os.path.abspath(filepath))
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump(cfg.to_dict(), tf, indent=2)
            temp_name = tf.name
        os.replace(temp_name, filepath)
        return True
    except Exception:
        return False

# Session State Init
if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()

if "last_alarmed_closure" not in st.session_state:
    st.session_state.last_alarmed_closure = None

if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

url_admin_token = st.query_params.get("admin", "")
if url_admin_token and verify_admin_password(url_admin_token):
    st.session_state.is_admin = True

if "temp_cfg" not in st.session_state:
    # We copy the config object using a new instance from dict to ensure separation
    st.session_state.temp_cfg = AppConfig.from_dict(st.session_state.cfg.to_dict())


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
        def wrapper(f): return f
        return wrapper

# ---------------------------------------------------------
# HTTP-SESSION MIT RETRY & POOLING
# ---------------------------------------------------------
@st.cache_resource
def get_http_session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

# ---------------------------------------------------------
# BAHNHOFSTAFEL CSS
# ---------------------------------------------------------
FIA_CSS = """
<style>
.fia-header { background-color: #ffcc00; color: #0b1a30; padding: 4px 0; display: flex; align-items: stretch; font-family: -apple-system, BlinkMacSystemFont, Arial, sans-serif; font-weight: bold; font-size: 1.05rem; }
.fia-row { background-color: #0b1a30; border-top: 1px solid #1a3258; padding: 4px 0; font-family: -apple-system, BlinkMacSystemFont, Arial, sans-serif; display: flex; align-items: stretch; color: #ffffff; box-sizing: border-box; }
.fia-time-col { width: 75px; flex-shrink: 0; padding: 6px 12px; display: flex; flex-direction: column; justify-content: flex-start; }
.fia-time { font-size: 1.35rem; font-weight: 700; color: #ffffff; line-height: 1; }
.fia-time-canc { font-size: 1.35rem; font-weight: 700; color: #ff5252; text-decoration: line-through; line-height: 1; }
.fia-delay { font-size: 0.85rem; font-weight: 700; color: #ffcc00; margin-top: 2px; }
.fia-ontime { font-size: 0.8rem; color: #4caf50; font-weight: 600; margin-top: 2px; }
.fia-canc-text { font-size: 0.85rem; font-weight: 700; color: #ff5252; text-transform: uppercase; margin-top: 2px; }
.fia-main-col { flex-grow: 1; min-width: 0; padding: 6px 12px; display: flex; flex-direction: column; border-left: 1px solid #1a3258; }
.fia-top-line { display: flex; align-items: baseline; margin-bottom: 2px; flex-wrap: wrap; }
.fia-train { font-size: 1.1rem; font-weight: normal; width: 65px; flex-shrink: 0; }
.fia-dest { font-size: 1.45rem; font-weight: bold; letter-spacing: -0.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fia-badge { background-color: #d32f2f; color: #ffffff; padding: 2px 6px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; margin-left: 12px; display: inline-flex; align-items: center; height: 20px; }
.fia-via { font-size: 0.85rem; color: #a0b6d6; line-height: 1.2; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fia-msg { color: #ffcc00; font-size: 0.85rem; font-weight: bold; margin-top: 4px; display: flex; align-items: flex-start; }
.fia-msg-icon { display: inline-block; width: 0; height: 0; border-left: 6px solid transparent; border-right: 6px solid transparent; border-bottom: 10px solid #ffcc00; margin-right: 6px; margin-top: 2px; }
.fia-plat-col { width: 65px; flex-shrink: 0; padding: 6px 4px; display: flex; flex-direction: column; align-items: center; justify-content: center; border-left: 1px solid #1a3258; }
.fia-plat-lbl { font-size: 0.65rem; text-transform: uppercase; font-weight: bold; color: #a0b6d6; letter-spacing: 1px; margin-bottom: 2px; }
.fia-plat-val { font-size: 1.5rem; font-weight: bold; line-height: 1; }
</style>
"""

# ---------------------------------------------------------
# CLIENT-SEITIGE LIVE-UHR & SANDBOX AUDIO
# ---------------------------------------------------------
def render_sidebar_clock():
    clock_html = """
    <div style="font-family: -apple-system, BlinkMacSystemFont, Roboto, Arial, sans-serif; font-size: 0.875rem; color: #555; padding: 2px 0 6px 0;">
        🕒 Aktuelle Uhrzeit: <span id="live-clock" style="color: #555;">--:--:-- Uhr</span>
    </div>
    <script>
    (function() {
        function updateClock() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('de-DE', { timeZone: 'Europe/Berlin', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const el = document.getElementById('live-clock');
            if (el) el.innerText = timeStr + " Uhr";
        }
        updateClock();
        setInterval(updateClock, 1000);
    })();
    </script>
    """
    components.html(clock_html, height=35)

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
            for (let i = 0; i < 4; i++) {
                const strikeTime = ctx.currentTime + (i * 0.55);
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
# ZENTRALE HELPER-FUNKTIONEN (ROUTING & NORMALISIERUNG)
# ---------------------------------------------------------
def clean_html(raw_html: str) -> str:
    return "\n".join(line.strip() for line in raw_html.splitlines() if line.strip())

def is_fernverkehr(tr: dict) -> bool:
    raw = (tr.get("train") or tr.get("line") or "").strip().upper()
    return any(raw.startswith(prefix) for prefix in ["ICE", "TGV", "IC", "EC", "ECE"])

def clean_train_name(tr: dict) -> str:
    raw = tr.get("line") or tr.get("train") or ""
    raw = str(raw).strip()
    m_dup = re.match(r'^(?:RE|RB|S|MEX|SWE|HLB)\s+((?:S|RE|RB)\s*\d+.*)$', raw, re.IGNORECASE)
    if m_dup: return re.sub(r'^(S|RE|RB)\s+(\d+)', r'\1\2', m_dup.group(1).strip())
    m_simple = re.match(r'^(S|RE|RB)\s+(\d+.*)$', raw, re.IGNORECASE)
    if m_simple: return f"{m_simple.group(1).upper()}{m_simple.group(2).strip()}"
    return raw if raw else "Zug"

def normalize_station_name(name: str) -> str:
    if not name: return ""
    return str(name).strip().lower().replace("ö", "oe").replace("ü", "ue").replace("ä", "ae")

def is_neustadt_hbf(name: str) -> bool:
    norm = normalize_station_name(name)
    return "neustadt" in norm and "boebig" not in norm and "sued" not in norm

def get_route_stops(tr: dict) -> List[str]:
    names = []
    for entry in tr.get("route", []):
        if isinstance(entry, dict): names.append(str(entry.get("name", "") or ""))
        elif isinstance(entry, str): names.append(entry)
    return names

def get_trip_destination(tr: dict) -> str:
    dest = tr.get("destination") or tr.get("direction") or ""
    if not dest:
        stops = get_route_stops(tr)
        if stops: dest = stops[-1]
    return str(dest).strip() if dest else "Neustadt(Weinstr)Hbf"

def get_trip_origin(tr: dict) -> str:
    origin = tr.get("origin") or ""
    if not origin:
        stops = get_route_stops(tr)
        if stops: origin = stops[0]
    return str(origin).strip() if origin else "Unbekannt"

def get_via_stops(tr: dict, mode: str) -> str:
    stops = get_route_stops(tr)
    if mode == "dep":
        post_stops = []
        found = False
        for s in stops:
            if found: post_stops.append(str(s))
            elif is_neustadt_hbf(s): found = True
        if not found: post_stops = [str(s) for s in stops]
        return ", ".join(post_stops[:5])
    else:
        pre_stops = []
        for s in stops:
            if is_neustadt_hbf(s): break
            pre_stops.append(str(s))
        return ", ".join(pre_stops[1:6]) if len(pre_stops) > 1 else ""

def parse_time(time_val, delay_min: int = 0, reference_dt: datetime.datetime = None) -> Optional[datetime.datetime]:
    if not time_val: return None
    match = re.search(r'(\d{1,2}):(\d{2})', str(time_val).strip())
    if not match: return None
    h, m = int(match.group(1)), int(match.group(2))
    ref = reference_dt or get_german_now()
    dt = ref.replace(hour=h, minute=m, second=0, microsecond=0)
    if delay_min:
        try: dt += datetime.timedelta(minutes=int(delay_min))
        except: pass
    if dt < ref - datetime.timedelta(hours=6): dt += datetime.timedelta(days=1)
    elif dt > ref + datetime.timedelta(hours=18): dt -= datetime.timedelta(days=1)
    return dt

@st.cache_data(show_spinner=False)
def fetch_dbf_board_cached(station_name: str, mode: str, use_realtime: bool, time_bucket: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    session = get_http_session()
    headers = {"User-Agent": f"Streamlit-Steingleis-Monitor/{APP_VERSION} (github.com/heis2201/Steingleis)"}
    safe_station = urllib.parse.quote(station_name)
    url = f"https://dbf.finalrewind.org/{safe_station}.json?version=3"
    if mode == "arr": url += "&mode=arr"
    if not use_realtime: url += "&rt=0"
    try:
        resp = session.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]: return [], f"API-Fehler ({station_name}): {data['error']}"
        return data.get("departures", []), None
    except Exception as e:
        return [], f"Verbindungsfehler ({station_name}): {e}"

def fetch_dbf_board(station_name: str, mode: str = "dep", use_realtime: bool = True, is_neighbor: bool = False) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    cfg = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg
    cache_ttl = cfg.api_cache_through_sec if is_neighbor else cfg.api_cache_local_sec
    now_epoch = int(datetime.datetime.now().timestamp())
    time_bucket = now_epoch // max(cache_ttl, 5)
    return fetch_dbf_board_cached(station_name, mode, use_realtime, time_bucket)

def check_crosses_steingleis(tr: dict, mode: str = "dep") -> bool:
    dest = get_trip_destination(tr)
    origin = get_trip_origin(tr)
    stops = get_route_stops(tr)
    
    if mode == "dep":
        post_stops = []
        found = False
        for s in stops:
            if found: post_stops.append(s)
            elif is_neustadt_hbf(s): found = True
        if not found: post_stops = stops
        
        return any(kw in normalize_station_name(dest) for kw in WEST_CORRIDOR_KEYWORDS) or                any(any(kw in normalize_station_name(s) for kw in WEST_CORRIDOR_KEYWORDS) for s in post_stops)
    else:
        pre_stops = []
        for s in stops:
            if is_neustadt_hbf(s): break
            pre_stops.append(s)
            
        return any(kw in normalize_station_name(origin) for kw in WEST_CORRIDOR_KEYWORDS) or                any(any(kw in normalize_station_name(s) for kw in WEST_CORRIDOR_KEYWORDS) for s in pre_stops)

def get_steingleis_events(use_realtime: bool, reference_dt: datetime.datetime) -> Tuple[List[ClosureEvent], list, list]:
    cfg = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg
    raw_intervals = []
    zugliste = []
    seen_events = set()
    neustadt_train_numbers = set()
    api_errors = []

    dep_data, dep_err = fetch_dbf_board(STATION_NAME, mode="dep", use_realtime=use_realtime, is_neighbor=False)
    arr_data, arr_err = fetch_dbf_board(STATION_NAME, mode="arr", use_realtime=use_realtime, is_neighbor=False)

    if dep_err: api_errors.append(dep_err)
    if arr_err: api_errors.append(arr_err)

    # 1. Abfahrten Neustadt
    for tr in dep_data:
        if tr.get("is_cancelled") or tr.get("isCancelled"): continue
        if check_crosses_steingleis(tr, mode="dep"):
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            dest = get_trip_destination(tr)
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0
            num_match = re.search(r'\d+', train_name)
            if num_match: neustadt_train_numbers.add(num_match.group(0))
            
            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                event_id = f"DEP_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = dep_dt - datetime.timedelta(seconds=cfg.lead_dep)
                    end = dep_dt + datetime.timedelta(seconds=cfg.lag_dep)
                    info = f"Abfahrt: {train_name} -> {dest}"
                    raw_intervals.append(ClosureEvent(start, end, [info]))
                    zugliste.append({
                        "Richtung": "➡️ Abfahrt (nach Westen)", "Zug": train_name, "Start/Ziel": dest,
                        "Fahrplanzeit": f'{dep_dt.strftime("%H:%M")} (+{delay})' if (use_realtime and delay>0) else dep_dt.strftime("%H:%M"),
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}", "_start_dt": start
                    })

    # 2. Ankünfte Neustadt
    kombinierte_ankuenfte = arr_data + dep_data
    for tr in kombinierte_ankuenfte:
        if tr.get("is_cancelled") or tr.get("isCancelled"): continue
        if check_crosses_steingleis(tr, mode="arr"):
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time") or tr.get("scheduledTime")
            if not sched_time: continue
            
            delay = int_or_default(tr.get("delayArrival", tr.get("delay", 0)), 0) if use_realtime else 0
            num_match = re.search(r'\d+', train_name)
            if num_match: neustadt_train_numbers.add(num_match.group(0))
            origin = get_trip_origin(tr)
            
            arr_dt = parse_time(sched_time, delay, reference_dt)
            if arr_dt:
                event_id = f"ARR_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = arr_dt - datetime.timedelta(seconds=cfg.lead_arr)
                    end = arr_dt - datetime.timedelta(seconds=cfg.lag_arr)
                    info = f"Ankunft: {train_name} aus {origin}"
                    raw_intervals.append(ClosureEvent(start, end, [info]))
                    zugliste.append({
                        "Richtung": "⬅️ Ankunft (aus Westen)", "Zug": train_name, "Start/Ziel": origin,
                        "Fahrplanzeit": f'{arr_dt.strftime("%H:%M")} (+{delay})' if (use_realtime and delay>0) else arr_dt.strftime("%H:%M"),
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}", "_start_dt": start
                    })

    # 3. Durchfahrten
    if cfg.track_through_trains:
        ma_dep_data, ma_err = fetch_dbf_board("Mannheim Hbf", mode="dep", use_realtime=use_realtime, is_neighbor=True)
        kl_dep_data, kl_err = fetch_dbf_board("Kaiserslautern Hbf", mode="dep", use_realtime=use_realtime, is_neighbor=True)
        if ma_err: api_errors.append(ma_err)
        if kl_err: api_errors.append(kl_err)

        for tr in ma_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"): continue
            dest = get_trip_destination(tr)
            stops = get_route_stops(tr)
            matches_west = any(kw in normalize_station_name(dest) for kw in WEST_BOUND_DESTS) or                            any(any(kw in normalize_station_name(s) for kw in WEST_BOUND_DESTS) for s in stops)
            if not matches_west or any(is_neustadt_hbf(s) for s in stops) or "neustadt" in normalize_station_name(dest): continue
            
            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers: continue
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0
            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                pass_dt = dep_dt + datetime.timedelta(minutes=cfg.transit_ma_min)
                event_id = f"THROUGH_WEST_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = pass_dt - datetime.timedelta(seconds=cfg.lead_dep)
                    end = pass_dt + datetime.timedelta(seconds=cfg.lag_dep)
                    info = f"Durchfahrt: {train_name} -> {dest}"
                    raw_intervals.append(ClosureEvent(start, end, [info]))
                    zugliste.append({
                        "Richtung": "➡️ Durchfahrt (nach Westen)", "Zug": train_name, "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')} (+{delay})" if (use_realtime and delay>0) else f"~{pass_dt.strftime('%H:%M')}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}", "_start_dt": start
                    })

        for tr in kl_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"): continue
            dest = get_trip_destination(tr)
            stops = get_route_stops(tr)
            matches_east = any(kw in normalize_station_name(dest) for kw in EAST_BOUND_DESTS) or                            any(any(kw in normalize_station_name(s) for kw in EAST_BOUND_DESTS) for s in stops)
            if not matches_east or any(is_neustadt_hbf(s) for s in stops) or "neustadt" in normalize_station_name(dest): continue
            
            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers: continue
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime")
            delay = int_or_default(tr.get("delayDeparture", tr.get("delay", 0)), 0) if use_realtime else 0
            dep_dt = parse_time(sched_time, delay, reference_dt)
            if dep_dt:
                pass_dt = dep_dt + datetime.timedelta(minutes=cfg.transit_kl_min)
                event_id = f"THROUGH_EAST_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = pass_dt - datetime.timedelta(seconds=cfg.lead_arr)
                    end = pass_dt + datetime.timedelta(seconds=cfg.lag_arr)
                    info = f"Durchfahrt: {train_name} -> {dest}"
                    raw_intervals.append(ClosureEvent(start, end, [info]))
                    zugliste.append({
                        "Richtung": "⬅️ Durchfahrt (aus Westen)", "Zug": train_name, "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')} (+{delay})" if (use_realtime and delay>0) else f"~{pass_dt.strftime('%H:%M')}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}", "_start_dt": start
                    })

    zugliste = sorted(zugliste, key=lambda x: x["_start_dt"])
    return raw_intervals, zugliste, api_errors

def merge_intervals(intervals: List[ClosureEvent], min_gap_sec: int) -> List[ClosureEvent]:
    if not intervals: return []
    sorted_intervals = sorted(intervals, key=lambda x: x.start)
    merged = []
    curr = sorted_intervals[0]
    
    for nxt in sorted_intervals[1:]:
        if nxt.start <= curr.end + datetime.timedelta(seconds=min_gap_sec):
            # Extend current interval
            curr.end = max(curr.end, nxt.end)
            # Add unique trains
            for t in nxt.trains:
                if t not in curr.trains:
                    curr.trains.append(t)
        else:
            merged.append(curr)
            curr = nxt
    merged.append(curr)
    return merged

def build_timeline_dataframe(base_dt: datetime.datetime, merged_intervals: List[ClosureEvent], duration_min: int = 60) -> pd.DataFrame:
    window_start = base_dt
    window_end = base_dt + datetime.timedelta(minutes=duration_min)
    events = []
    curr = window_start
    sorted_int = sorted(merged_intervals, key=lambda x: x.start)
    for ev in sorted_int:
        if ev.end <= window_start or ev.start >= window_end: continue
        eff_start = max(window_start, ev.start)
        eff_end = min(window_end, ev.end)
        if eff_start > curr:
            events.append({"Start": curr, "Ende": eff_start, "Status": "Freie Fahrt (Offen)", "Details": "BÜ geöffnet"})
        events.append({"Start": eff_start, "Ende": eff_end, "Status": "Schranke Geschlossen", "Details": " + ".join(ev.trains)})
        curr = max(curr, eff_end)
    if curr < window_end:
        events.append({"Start": curr, "Ende": window_end, "Status": "Freie Fahrt (Offen)", "Details": "BÜ geöffnet"})
    return pd.DataFrame(events)

# ---------------------------------------------------------
# BAHNHOFSTAFEL HELPER IM DB-LOOK (FIA)
# ---------------------------------------------------------
def render_db_board_card(tr: dict, mode: str = "dep", use_realtime: bool = True):
    is_cancelled = bool(tr.get("is_cancelled") or tr.get("isCancelled"))
    train_name = clean_train_name(tr)
    
    if mode == "dep":
        sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time") or tr.get("scheduledTime") or "--:--"
        delay_raw = tr.get("delayDeparture")
    else:
        sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time") or tr.get("scheduledTime") or "--:--"
        delay_raw = tr.get("delayArrival")
        
    if delay_raw is None: delay_raw = tr.get("delay", 0)
    delay = int_or_default(delay_raw, 0) if use_realtime else 0
    platform = tr.get("platform") or tr.get("scheduledPlatform") or "-"

    main_station = get_trip_destination(tr) if mode == "dep" else get_trip_origin(tr)
    via_label = get_via_stops(tr, mode)
    crosses_bu = check_crosses_steingleis(tr, mode=mode)

    if is_cancelled:
        time_html = f'<div class="fia-time-canc">{sched_time}</div><div class="fia-canc-text">Fällt aus</div>'
    elif use_realtime and delay > 0:
        time_html = f'<div class="fia-time">{sched_time}</div><div class="fia-delay">+{delay} min</div>'
    elif use_realtime:
        time_html = f'<div class="fia-time">{sched_time}</div><div class="fia-ontime">Pünktlich</div>'
    else:
        time_html = f'<div class="fia-time">{sched_time}</div>'

    bu_badge = '<span class="fia-badge"><span style="font-size: 0.85rem; margin-right: 4px;">🚦</span> BÜ Steingleis</span>' if crosses_bu else ''

    messages = tr.get("messages", {})
    msg_list = []
    if isinstance(messages, dict):
        for m in messages.values():
            if isinstance(m, list):
                for item in m:
                    if isinstance(item, dict) and "text" in item: msg_list.append(item["text"])
                    elif isinstance(item, str): msg_list.append(item)
    elif isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict) and "text" in item: msg_list.append(item["text"])
            elif isinstance(item, str): msg_list.append(item)
    msg_str = " | ".join(msg_list) if msg_list else ""
    
    msg_html = f'<div class="fia-msg"><span class="fia-msg-icon"></span><span>{msg_str}</span></div>' if msg_str else ''
    via_html = f'<div class="fia-via">{via_label}</div>' if via_label else ''

    card_html = (
        f'<div class="fia-row">'
        f'<div class="fia-time-col">{time_html}</div>'
        f'<div class="fia-main-col">'
        f'<div class="fia-top-line">'
        f'<div class="fia-train">{train_name}</div>'
        f'<div class="fia-dest">{main_station}</div>'
        f'{bu_badge}'
        f'</div>'
        f'{via_html}'
        f'{msg_html}'
        f'</div>'
        f'<div class="fia-plat-col">'
        f'<div class="fia-plat-lbl">Gleis</div>'
        f'<div class="fia-plat-val">{platform}</div>'
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
            cfg_to_read = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg
            ui_interval = cfg_to_read.ui_refresh_sec
            auto_refresh = st.toggle(f"🔄 Auto-Refresh ({ui_interval}s)", value=True)
            plan_selected_dt = None
        else:
            use_realtime = False
            auto_refresh = False
            st.caption("Echtzeit-Verspätungen deaktiviert (reiner Soll-Fahrplan).")
            col_d, col_t = st.columns(2)
            sel_date = col_d.date_input("Datum:", value=now_german.date())
            sel_time = col_t.time_input("Uhrzeit:", value=now_german.time())
            plan_selected_dt = datetime.datetime.combine(sel_date, sel_time)

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
    cfg_to_read = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg
    refresh_interval = cfg_to_read.ui_refresh_sec if (use_realtime and auto_refresh) else None

    @fragment_dec(run_every=refresh_interval)
    def render_live_dashboard():
        current_time_tick = get_german_now()
        calc_base_dt = current_time_tick if use_realtime else plan_selected_dt

        raw_intervals, zugliste, api_errors = get_steingleis_events(use_realtime, calc_base_dt)
        merged = merge_intervals(raw_intervals, cfg_to_read.min_gap)

        st.title("🚦 BÜ Steingleis (Neustadt/Weinstraße)")
        if not use_realtime:
            st.caption(f"🕒 Gewählter Analysezeitpunkt: **{calc_base_dt.strftime('%d.%m.%Y um %H:%M')} Uhr**")

        if api_errors:
            with st.expander("⚠️ Verbindungswarnung zur Fahrplan-API", expanded=True):
                for err in api_errors: st.warning(err)
                st.caption("Aufgrund von Verbindungsstörungen können Schließungen fehlen.")

        is_closed = False
        current_start = None
        current_end = None
        next_closure = None
        active_reasons = []

        for ev in merged:
            if ev.start <= calc_base_dt <= ev.end:
                is_closed = True
                current_start = ev.start
                current_end = ev.end
                active_reasons = ev.trains
                break
            elif ev.start > calc_base_dt:
                if next_closure is None or ev.start < next_closure.start:
                    next_closure = ev

        if use_realtime and cfg_to_read.sound_alarm:
            sound_warn_sec = cfg_to_read.sound_warn_sec
            if next_closure:
                closure_id = next_closure.start.strftime("%Y-%m-%d %H:%M:%S")
                sec_to_next = (next_closure.start - calc_base_dt).total_seconds()
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
                st.metric("Geschlossen bis ca.", current_end.strftime("%H:%M:%S"), f"Noch {rest_sec // 60}m {rest_sec % 60:02d}s")
                st.caption("Auslöser:")
                for r in active_reasons: st.write(f"- {r}")
            else:
                st.success("### ✅ SCHRANKE OFFEN (FREIE FAHRT)")
                if next_closure:
                    diff_sec = int((next_closure.start - calc_base_dt).total_seconds())
                    st.metric("Nächste Schließung um", next_closure.start.strftime("%H:%M:%S"), f"In ca. {diff_sec // 60}m {diff_sec % 60:02d}s")
                    st.caption(f"Nächster Zug: {next_closure.trains[0]}")
                else:
                    st.info("Im analysierten Zeitfenster stehen keine Schließungen an.")

        with col2:
            st.subheader("📋 Prognostizierte Schließphasen")
            future_rows = []
            for ev in merged:
                if ev.end >= calc_base_dt:
                    dur = int((ev.end - ev.start).total_seconds())
                    future_rows.append({"Von": ev.start.strftime("%H:%M:%S"), "Bis": ev.end.strftime("%H:%M:%S"), "Dauer": f"{dur // 60}m {dur % 60:02d}s", "Betroffene Züge": " + ".join(ev.trains)})
            if future_rows: st.dataframe(pd.DataFrame(future_rows), width="stretch", hide_index=True)
            else: st.write("Keine Schließungen im gewählten Zeitabschnitt.")

        st.markdown("---")
        st.subheader("⏱️ Zeitstrahl nächste 60 Minuten")
        df_timeline = build_timeline_dataframe(calc_base_dt, merged, duration_min=60)
        
        if not df_timeline.empty:
            window_end = calc_base_dt + datetime.timedelta(minutes=60)
            chart = (
                alt.Chart(df_timeline).mark_bar(cornerRadius=4, height=36).encode(
                    x=alt.X('Start:T', title='Uhrzeit (5-Minuten-Takt)', scale=alt.Scale(domain=[calc_base_dt, window_end]), axis=alt.Axis(format='%H:%M', tickCount=12, labelAngle=0, labelFontSize=11, grid=True, gridDash=[2, 2], orient='bottom')),
                    x2='Ende:T', color=alt.Color('Status:N', scale=alt.Scale(domain=['Freie Fahrt (Offen)', 'Schranke Geschlossen'], range=['#22c55e', '#ef4444']), legend=alt.Legend(title=None, orient='top', labelFontSize=12)),
                    tooltip=[alt.Tooltip('Status:N', title='Status'), alt.Tooltip('Start:T', format='%H:%M:%S', title='Von'), alt.Tooltip('Ende:T', format='%H:%M:%S', title='Bis'), alt.Tooltip('Details:N', title='Details / Züge')]
                ).properties(height=125)
            )
            st.altair_chart(chart, width="stretch")

        with st.expander(f"🔍 Alle erfassten Züge am Steingleis ({len(zugliste)} Fahrten)"):
            if zugliste: st.dataframe(pd.DataFrame(zugliste).drop(columns=["_start_dt"]), width="stretch", hide_index=True)
            else: st.write("Keine Fahrten gefunden.")

    render_live_dashboard()

# =========================================================
# SEITE 2: BAHNHOFSTAFEL NEUSTADT (WEINSTR) HBF
# =========================================================
elif active_page == "🚉 Bahnhofstafel":
    st.markdown(FIA_CSS, unsafe_allow_html=True)
    cfg_to_read = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg
    refresh_interval = cfg_to_read.ui_refresh_sec if (use_realtime and auto_refresh) else None

    @fragment_dec(run_every=refresh_interval)
    def render_station_board():
        st.title("🚉 Bahnhofstafel Neustadt (Weinstr) Hbf")
        mode_label = "🔴 Live-Echtzeit" if use_realtime else f"📅 Plan-Stand ({plan_selected_dt.strftime('%d.%m.%Y %H:%M')} Uhr)"
        st.caption(f"Digitale Fahrgastinformation via DBF • {mode_label}")

        dep_data, dep_err = fetch_dbf_board(STATION_NAME, mode="dep", use_realtime=use_realtime, is_neighbor=False)
        arr_data, arr_err = fetch_dbf_board(STATION_NAME, mode="arr", use_realtime=use_realtime, is_neighbor=False)

        if dep_err or arr_err:
            with st.expander("⚠️ Verbindungsstatus zur API", expanded=True):
                if dep_err: st.warning(dep_err)
                if arr_err: st.warning(arr_err)

        tab_dep, tab_arr = st.tabs(["➡️ Abfahrten", "⬅️ Ankünfte"])

        with tab_dep:
            col_f1, col_f2 = st.columns([1, 2])
            with col_f1: filter_steingleis_dep = st.checkbox("Nur Züge über BÜ Steingleis", value=False, key="filter_dep_steingleis")
            with col_f2: search_dep = st.text_input("🔍 Filter (Zug, Ziel):", "", key="search_dep")

            filtered_dep = []
            for tr in dep_data:
                sched_dep = tr.get("scheduledDeparture") or tr.get("departure")
                if not sched_dep: continue

                dest = get_trip_destination(tr)
                train_name = clean_train_name(tr)
                if filter_steingleis_dep and not check_crosses_steingleis(tr, mode="dep"): continue
                if search_dep:
                    s_low = search_dep.strip().lower()
                    if s_low not in dest.lower() and s_low not in train_name.lower(): continue
                filtered_dep.append(tr)

            if filtered_dep:
                header_html = '<div class="fia-header"><div style="width: 75px; flex-shrink: 0; padding: 2px 12px;">Zeit</div><div style="flex-grow: 1; min-width: 0; padding: 2px 12px; display: flex;"><div style="width: 65px; flex-shrink: 0;">Zug</div><div>Nach / Über</div></div><div style="width: 65px; flex-shrink: 0; padding: 2px 4px; text-align: center;">Gleis</div></div>'
                st.markdown(clean_html(header_html), unsafe_allow_html=True)
                for tr in filtered_dep: render_db_board_card(tr, mode="dep", use_realtime=use_realtime)
            else:
                st.info("Keine Abfahrten für die aktuellen Filterkriterien gefunden.")

        with tab_arr:
            col_fa1, col_fa2 = st.columns([1, 2])
            with col_fa1: filter_steingleis_arr = st.checkbox("Nur Züge über BÜ Steingleis", value=False, key="filter_arr_steingleis")
            with col_fa2: search_arr = st.text_input("🔍 Filter (Zug, Herkunft):", "", key="search_arr")

            filtered_arr = []
            for tr in arr_data:
                sched_arr = tr.get("scheduledArrival") or tr.get("arrival")
                if not sched_arr: continue

                origin = get_trip_origin(tr)
                train_name = clean_train_name(tr)
                if filter_steingleis_arr and not check_crosses_steingleis(tr, mode="arr"): continue
                if search_arr:
                    s_low = search_arr.strip().lower()
                    if s_low not in origin.lower() and s_low not in train_name.lower(): continue
                filtered_arr.append(tr)

            if filtered_arr:
                header_html = '<div class="fia-header"><div style="width: 75px; flex-shrink: 0; padding: 2px 12px;">Zeit</div><div style="flex-grow: 1; min-width: 0; padding: 2px 12px; display: flex;"><div style="width: 65px; flex-shrink: 0;">Zug</div><div>Aus Richtung</div></div><div style="width: 65px; flex-shrink: 0; padding: 2px 4px; text-align: center;">Gleis</div></div>'
                st.markdown(clean_html(header_html), unsafe_allow_html=True)
                for tr in filtered_arr: render_db_board_card(tr, mode="arr", use_realtime=use_realtime)
            else:
                st.info("Keine Ankünfte für die aktuellen Filterkriterien gefunden.")

    render_station_board()

# =========================================================
# SEITE 3: EINSTELLUNGEN
# =========================================================
elif active_page == "⚙️ Einstellungen":
    st.title("⚙️ Einstellungen & Parameter")
    st.caption(f"Version {APP_VERSION} • Die hier getätigten Eingaben wirken zunächst nur für Sie (Vorschau-Modus). Dauerhaftes Speichern auf dem Server erfordert ein Passwort.")

    if st.session_state.get("config_load_error"):
        st.warning("⚠️ Die serverseitige `config.json` war fehlerhaft/unlesbar und wurde durch Standardwerte ersetzt.")

    cfg = st.session_state.cfg if st.session_state.is_admin else st.session_state.temp_cfg

    tab1, tab2, tab3, tab_admin = st.tabs(["🛠️ Stellwerks-Parameter", "🚄 Fernverkehr & Durchfahrten", "⏱️ Taktung, API & Audio", "🔒 System-Admin"])

    with tab1:
        st.subheader("Vor- und Nachlaufzeiten am BÜ Steingleis")
        c1, c2 = st.columns(2)
        with c1:
            cfg.lead_dep = st.slider("Schließung vor Abfahrt Hbf (Sek.)", 30, 360, cfg.lead_dep, step=10, help="Vorlaufzeit vor Planabfahrt am Hbf inklusive Signal- und Fahrstraßenstellung.")
            cfg.lag_dep = st.slider("Öffnung nach Abfahrt Hbf (Sek.)", 30, 180, cfg.lag_dep, step=10, help="Zeit bis der abfahrende Zug den BÜ komplett geräumt hat.")
        with c2:
            cfg.lead_arr = st.slider("Schließung vor Ankunft Hbf (Sek.)", 60, 240, cfg.lead_arr, step=10, help="Zeitspanne vor Planankunft am Bahnsteig, wenn die Schranke fällt.")
            cfg.lag_arr = st.slider("Öffnung vor Ankunft Hbf (Sek.)", 10, 90, cfg.lag_arr, step=5, help="Zeit vor Ankunft am Bahnsteig, zu der die Zugspitze den BÜ bereits passiert hat.")
        cfg.min_gap = st.slider("Mindestöffnungslücke (Sek.)", 0, 120, cfg.min_gap, step=10, help="Ist die Zeit zwischen Öffnung und nächster Schließung kürzer als dieser Wert, bleibt die Schranke geschlossen.")

    with tab2:
        st.subheader("Fernverkehrs-Laufzeitüberwachung (KBS 670)")
        cfg.track_through_trains = st.toggle("ICE- und TGV-Durchfahrten ohne Halt in Neustadt erfassen", value=cfg.track_through_trains)
        col_ma, col_kl = st.columns(2)
        with col_ma: cfg.transit_ma_min = st.slider("Fahrzeit Mannheim Hbf -> BÜ Steingleis (Min.)", 10, 25, cfg.transit_ma_min, step=1)
        with col_kl: cfg.transit_kl_min = st.slider("Fahrzeit Kaiserslautern Hbf -> BÜ Steingleis (Min.)", 12, 30, cfg.transit_kl_min, step=1)

    with tab3:
        st.subheader("⏱️ Taktung & API-Schonung")
        c_ui, c_api1, c_api2 = st.columns(3)
        with c_ui: cfg.ui_refresh_sec = st.slider("🖥️ UI-Neuberechnung (Sek.)", 2, 30, cfg.ui_refresh_sec, step=1)
        with c_api1: cfg.api_cache_local_sec = st.slider("📡 DB-Abruf Neustadt (Sek.)", 10, 120, cfg.api_cache_local_sec, step=5)
        with c_api2: cfg.api_cache_through_sec = st.slider("🚄 DB-Abruf Fernverkehr (Sek.)", 15, 180, cfg.api_cache_through_sec, step=15)
        st.markdown("---")
        st.subheader("🔔 Akustische Warnung")
        col_snd1, col_snd2 = st.columns(2)
        with col_snd1:
            cfg.sound_alarm = st.toggle("Schrankengong aktivieren", value=cfg.sound_alarm)
            cfg.sound_warn_sec = st.slider("Vorwarnzeit vor Schließung (Sek.)", 15, 120, cfg.sound_warn_sec, step=5)
        with col_snd2:
            if st.button("🔊 Signalton jetzt testen"): play_sound_cloud()

    with tab_admin:
        st.subheader("🔒 Server-Konfiguration speichern")
        if st.session_state.is_admin:
            st.success("🔓 Sie sind als Administrator authentifiziert. Einstellungen können direkt gespeichert werden.")
            pwd_input = None
        else:
            st.write("Um die aktuellen Schieberegler-Werte dauerhaft für **alle Besucher** zu übernehmen, wird das Admin-Passwort benötigt.")
            pwd_input = st.text_input("Admin-Passwort:", type="password", key="admin_pwd_input")
        
        col_save, col_reset = st.columns([1, 1])
        with col_save:
            if st.button("💾 Dauerhaft in config.json speichern", type="primary", width="stretch"):
                is_authorized = st.session_state.is_admin or (pwd_input and verify_admin_password(pwd_input))
                if is_authorized:
                    st.session_state.cfg = AppConfig.from_dict(st.session_state.temp_cfg.to_dict())
                    success = save_config_atomic(st.session_state.cfg)
                    if success:
                        st.session_state["config_load_error"] = False
                        st.session_state.is_admin = True
                        st.success("✅ Zugriff gewährt: Einstellungen dauerhaft auf dem Server gespeichert!")
                    else:
                        st.error("❌ Fehler beim atomaren Speichern auf dem Server.")
                else:
                    st.error("❌ Falsches Passwort oder keine Berechtigung.")

        with col_reset:
            if st.button("↩️ Vorschau-Modus zurücksetzen (Neu laden)", width="stretch"):
                st.session_state.temp_cfg = AppConfig.from_dict(st.session_state.cfg.to_dict())
                st.session_state.is_admin = False
                st.info("Ihre temporären Änderungen wurden verworfen. Lade Server-Standard...")
                st.rerun()
