import datetime
import json
import os
import re
import urllib.parse
from zoneinfo import ZoneInfo
from typing import List, Tuple, Dict, Any
import requests
import streamlit as st
import pandas as pd
import altair as alt

# ---------------------------------------------------------
# ANWENDUNGS-VERSION & KONFIGURATION
# ---------------------------------------------------------
APP_VERSION = "0.1.2"

st.set_page_config(
    page_title=f"BÜ Steingleis Monitor v{APP_VERSION}",
    page_icon="🚦",
    layout="wide"
)

# Zwingend für Streamlit Cloud: UTC-Serverzeit auf deutsche Zeit umrechnen
BERLIN_TZ = ZoneInfo("Europe/Berlin")

def get_german_now() -> datetime.datetime:
    """Liefert die exakte deutsche Ortszeit als naive datetime (für korrekte Berechnungen)."""
    return datetime.datetime.now(BERLIN_TZ).replace(tzinfo=None)

CONFIG_FILE = "config.json"
STATION_NAME = "Neustadt(Weinstr)Hbf"

DEFAULT_CONFIG = {
    "refresh_interval_sec": 15,
    "sound_alarm": True,
    "sound_warn_sec": 45,
    "track_through_trains": True,
    "transit_ma_min": 16,
    "transit_kl_min": 19,
    "lead_dep": 240,
    "lag_dep": 90,
    "lead_arr": 160,
    "lag_arr": 40,
    "min_gap": 30
}

def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {**DEFAULT_CONFIG, **data}
        except Exception:
            return DEFAULT_CONFIG
    return DEFAULT_CONFIG

if "cfg" not in st.session_state:
    st.session_state.cfg = load_config()

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
# CLOUD-SICHERER AUDIO-ALARM
# ---------------------------------------------------------
def play_sound_cloud():
    """Führt die Web Audio API isoliert aus, ohne Cross-Origin-Sicherheitsregeln zu verletzen."""
    js_code = """
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
    """
    if hasattr(st, "html"):
        st.html(js_code)
    else:
        st.markdown(js_code, unsafe_allow_html=True)

# ---------------------------------------------------------
# HILFSFUNKTIONEN & FILTER
# ---------------------------------------------------------
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

def extract_station_names(route_list) -> List[str]:
    names = []
    for entry in route_list:
        if isinstance(entry, dict):
            names.append(entry.get("name", ""))
        elif isinstance(entry, str):
            names.append(entry)
    return names

def parse_time(time_val, delay_min: int = 0, reference_dt: datetime.datetime = None) -> datetime.datetime:
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

def fetch_dbf_board(station_name: str, mode: str = "dep", use_realtime: bool = True) -> Tuple[List[Dict[str, Any]], str]:
    headers = {"User-Agent": "Streamlit-Steingleis-Monitor-Cloud"}
    safe_station = urllib.parse.quote(station_name)
    url = f"https://dbf.finalrewind.org/{safe_station}.json?version=3"
    if mode == "arr":
        url += "&mode=arr"
    if not use_realtime:
        url += "&rt=0"

    try:
        resp = requests.get(url, headers=headers, timeout=6)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]:
            return [], f"API: {data['error']}"
        return data.get("departures", []), ""
    except Exception as e:
        return [], f"Verbindungsfehler: {e}"

@st.cache_data(ttl=60, show_spinner=False)
def fetch_cached_neighbor_board(station_name: str) -> List[Dict[str, Any]]:
    data, _ = fetch_dbf_board(station_name, mode="dep", use_realtime=True)
    return data

def get_steingleis_events(use_realtime: bool, reference_dt: datetime.datetime):
    cfg = st.session_state.cfg
    lead_dep = int(cfg["lead_dep"])
    lag_dep = int(cfg["lag_dep"])
    lead_arr = int(cfg["lead_arr"])
    lag_arr = int(cfg["lag_arr"])
    track_through = bool(cfg["track_through_trains"])
    transit_ma = int(cfg["transit_ma_min"])
    transit_kl = int(cfg["transit_kl_min"])

    raw_intervals = []
    zugliste = []
    seen_events = set()
    neustadt_train_numbers = set()

    dep_data, _ = fetch_dbf_board(STATION_NAME, mode="dep", use_realtime=use_realtime)
    arr_data, _ = fetch_dbf_board(STATION_NAME, mode="arr", use_realtime=use_realtime)

    # 1. Abfahrten ab Neustadt (nach Westen)
    for tr in dep_data:
        if tr.get("is_cancelled") or tr.get("isCancelled"):
            continue

        dest = tr.get("destination") or tr.get("direction") or ""
        route_stops = extract_station_names(tr.get("route", []))

        post_stops = []
        found_neustadt = False
        for s in route_stops:
            if found_neustadt:
                post_stops.append(s)
            elif "neustadt" in s.lower():
                found_neustadt = True
        if not found_neustadt:
            post_stops = route_stops

        is_west = any(kw in dest.lower() for kw in WEST_CORRIDOR_KEYWORDS) or \
                  any(any(kw in s.lower() for kw in WEST_CORRIDOR_KEYWORDS) for s in post_stops)

        if is_west:
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time")
            delay = tr.get("delayDeparture", 0) if use_realtime else 0

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
                    delay_str = f" (+{delay})" if (use_realtime and delay) else ""
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

        route_stops = extract_station_names(tr.get("route", []))
        pre_stops = []
        for s in route_stops:
            if "neustadt" in s.lower():
                break
            pre_stops.append(s)

        origin = pre_stops[0] if pre_stops else tr.get("origin", "")
        is_from_west = any(kw in origin.lower() for kw in WEST_CORRIDOR_KEYWORDS) or \
                       any(any(kw in s.lower() for kw in WEST_CORRIDOR_KEYWORDS) for s in pre_stops)

        if is_from_west:
            train_name = clean_train_name(tr)
            sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time")
            delay = tr.get("delayArrival", 0) if use_realtime else 0

            num_match = re.search(r'\d+', train_name)
            if num_match:
                neustadt_train_numbers.add(num_match.group(0))

            arr_dt = parse_time(sched_time, delay, reference_dt)
            if arr_dt:
                event_id = f"ARR_{train_name}_{sched_time}"
                if event_id not in seen_events:
                    seen_events.add(event_id)
                    start = arr_dt - datetime.timedelta(seconds=lead_arr)
                    end = arr_dt - datetime.timedelta(seconds=lag_arr)

                    herkunft = origin if origin else "Pfälzerwald"
                    info = f"Ankunft: {train_name} aus {herkunft}"
                    raw_intervals.append((start, end, info))
                    delay_str = f" (+{delay})" if (use_realtime and delay) else ""
                    zugliste.append({
                        "Richtung": "⬅️ Ankunft (aus Westen)",
                        "Zug": train_name,
                        "Start/Ziel": herkunft,
                        "Fahrplanzeit": arr_dt.strftime("%H:%M") + delay_str,
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

    # 3. Durchfahrten (ICE/TGV)
    if track_through:
        ma_dep_data = fetch_cached_neighbor_board("Mannheim Hbf")
        kl_dep_data = fetch_cached_neighbor_board("Kaiserslautern Hbf")

        # 3.1 Mannheim -> West
        for tr in ma_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"):
                continue

            dest = tr.get("destination") or tr.get("direction") or ""
            route_stops = extract_station_names(tr.get("route", []))

            matches_west = any(kw in dest.lower() for kw in WEST_BOUND_DESTS) or \
                           any(any(kw in s.lower() for kw in WEST_BOUND_DESTS) for s in route_stops)
            if not matches_west:
                continue

            if any("neustadt" in s.lower() for s in route_stops) or "neustadt" in dest.lower():
                continue

            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers:
                continue

            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time")
            delay = tr.get("delayDeparture", 0) if use_realtime else 0

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
                    delay_str = f" (+{delay})" if (use_realtime and delay) else ""
                    zugliste.append({
                        "Richtung": "➡️ Durchfahrt (nach Westen)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')}{delay_str}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

        # 3.2 Kaiserslautern -> Ost
        for tr in kl_dep_data:
            if not is_fernverkehr(tr) or tr.get("is_cancelled") or tr.get("isCancelled"):
                continue

            dest = tr.get("destination") or tr.get("direction") or ""
            route_stops = extract_station_names(tr.get("route", []))

            matches_east = any(kw in dest.lower() for kw in EAST_BOUND_DESTS) or \
                           any(any(kw in s.lower() for kw in EAST_BOUND_DESTS) for s in route_stops)
            if not matches_east:
                continue

            if any("neustadt" in s.lower() for s in route_stops) or "neustadt" in dest.lower():
                continue

            train_name = clean_train_name(tr)
            num_match = re.search(r'\d+', train_name)
            if num_match and num_match.group(0) in neustadt_train_numbers:
                continue

            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time")
            delay = tr.get("delayDeparture", 0) if use_realtime else 0

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
                    delay_str = f" (+{delay})" if (use_realtime and delay) else ""
                    zugliste.append({
                        "Richtung": "⬅️ Durchfahrt (aus Westen)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": f"~{pass_dt.strftime('%H:%M')}{delay_str}",
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

    zugliste = sorted(zugliste, key=lambda x: x["_start_dt"])
    return raw_intervals, zugliste

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
# SEITEN-NAVIGATION (SIDEBAR)
# ---------------------------------------------------------
with st.sidebar:
    st.title("🚦 BÜ Steingleis")
    st.caption(f"App-Version {APP_VERSION}")
    
    active_page = st.radio("Bereich wählen:", ["🚦 Monitor", "⚙️ Einstellungen"], index=0)
    st.markdown("---")

    # Modus & Zeit (nur auf der Monitor-Seite anzeigen)
    if active_page == "🚦 Monitor":
        st.subheader("Modus & Zeit")
        modus = st.radio("Betriebsmodus:", ["🔴 Live-Echtzeit (Jetzt)", "📅 Fahrplan-Analyse (Wunschzeit)"])
        now_german = get_german_now()

        if modus == "🔴 Live-Echtzeit (Jetzt)":
            use_realtime = True
            auto_refresh = st.toggle(
                f"🔄 Auto-Refresh ({st.session_state.cfg.get('refresh_interval_sec', 15)}s)", 
                value=True
            )
            plan_selected_time = None
        else:
            use_realtime = False
            auto_refresh = False
            st.caption("Echtzeit-Verspätungen deaktiviert (reiner Soll-Fahrplan).")
            plan_selected_time = st.time_input("Wunsch-Uhrzeit:", value=now_german.time())

        st.caption(f"🕒 Referenzzeit: **{now_german.strftime('%H:%M:%S')}** (MEZ/MESZ)")

        if st.button("🔄 Jetzt aktualisieren", width="stretch"):
            st.rerun()

    # Footer ganz unten in der Sidebar
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
    refresh_interval = int(st.session_state.cfg.get("refresh_interval_sec", 15)) if (use_realtime and auto_refresh) else None

    @fragment_dec(run_every=refresh_interval)
    def render_live_dashboard():
        current_time_tick = get_german_now()
        
        if use_realtime:
            calc_base_dt = current_time_tick
        else:
            calc_base_dt = current_time_tick.replace(
                hour=plan_selected_time.hour, 
                minute=plan_selected_time.minute, 
                second=0
            )

        raw_intervals, zugliste = get_steingleis_events(use_realtime, calc_base_dt)
        min_gap = int(st.session_state.cfg.get("min_gap", 30))
        merged = merge_intervals(raw_intervals, min_gap)

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

        # Akustischer Alarm (cloud-sicher)
        if use_realtime and st.session_state.cfg.get("sound_alarm", True):
            sound_warn_sec = int(st.session_state.cfg.get("sound_warn_sec", 45))
            should_alarm = False
            if next_closure:
                sec_to_next = (next_closure[0] - calc_base_dt).total_seconds()
                if 0 < sec_to_next <= sound_warn_sec:
                    should_alarm = True
            elif is_closed and current_start:
                sec_since_start = (calc_base_dt - current_start).total_seconds()
                if 0 <= sec_since_start <= 15:
                    should_alarm = True

            if should_alarm:
                play_sound_cloud()

        # Statuskacheln
        st.title(f"🚦 BÜ Steingleis (Neustadt/Weinstraße)")
        st.caption(f"🕒 Aktuelle Uhrzeit (MEZ/MESZ): **{current_time_tick.strftime('%H:%M:%S')} Uhr**")
        col1, col2 = st.columns([1, 1])

        with col1:
            sub_title = (
                f"Live-Status (Jetzt)" 
                if use_realtime 
                else f"Status um {calc_base_dt.strftime('%H:%M')} Uhr (Plan)"
            )
            st.subheader(sub_title)
            
            if is_closed:
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
                    st.caption(f"Erster Zug: {tr_next[0]}")
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

        # Zugliste
        with st.expander(f"🔍 Alle erfassten Züge am Steingleis ({len(zugliste)} Fahrten)"):
            if zugliste:
                df_display = pd.DataFrame(zugliste).drop(columns=["_start_dt"])
                st.dataframe(df_display, width="stretch", hide_index=True)
            else:
                st.write("Keine Fahrten gefunden.")

    render_live_dashboard()

# =========================================================
# SEITE 2: EINSTELLUNGEN
# =========================================================
elif active_page == "⚙️ Einstellungen":
    st.title("⚙️ Einstellungen & Parameter")
    st.caption(f"Version {APP_VERSION} • Konfiguration des Stellwerksmodells, der Durchfahrten und der Signale. Änderungen wirken sofort.")

    cfg = st.session_state.cfg

    tab1, tab2, tab3 = st.tabs(["🛠️ Stellwerks-Parameter", "🚄 Fernverkehr & Durchfahrten", "🔔 Ton & Auto-Refresh"])

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
            help="Ist die Zeit zwischen Öffnung und nächster Schließung kürzer als dieser Wert, bleibt die Schranke dauerhaft geschlossen."
        )

    with tab2:
        st.subheader("Fernverkehrs-Laufzeitüberwachung (KBS 670)")
        cfg["track_through_trains"] = st.toggle(
            "ICE- und TGV-Durchfahrten ohne Halt in Neustadt erfassen",
            value=bool(cfg.get("track_through_trains", True)),
            help="Überwacht die Nachbarknoten Mannheim Hbf und Kaiserslautern Hbf auf Züge Richtung Paris / Frankfurt."
        )

        col_ma, col_kl = st.columns(2)
        with col_ma:
            cfg["transit_ma_min"] = st.slider(
                "Fahrzeit Mannheim Hbf -> BÜ Steingleis (Min.)", 10, 25, int(cfg.get("transit_ma_min", 16)), step=1,
                help="Typische Reisezeit eines ICE/TGV durch die Rheinebene bis zum BÜ."
            )
        with col_kl:
            cfg["transit_kl_min"] = st.slider(
                "Fahrzeit Kaiserslautern Hbf -> BÜ Steingleis (Min.)", 12, 30, int(cfg.get("transit_kl_min", 19)), step=1,
                help="Typische Reisezeit eines ICE/TGV durch den Pfälzerwald bis zum BÜ."
            )

    with tab3:
        st.subheader("Signaltöne & Aktualisierungsintervall")
        col_snd, col_ref = st.columns(2)
        with col_snd:
            cfg["sound_alarm"] = st.toggle("Schrankengong aktivieren", value=bool(cfg.get("sound_alarm", True)))
            cfg["sound_warn_sec"] = st.slider(
                "Vorwarnzeit vor Schließung (Sek.)", 15, 120, int(cfg.get("sound_warn_sec", 45)), step=5
            )
            if st.button("🔊 Signalton jetzt testen"):
                play_sound_cloud()

        with col_ref:
            cfg["refresh_interval_sec"] = st.slider(
                "Auto-Refresh-Intervall (Sek.)", 10, 60, int(cfg.get("refresh_interval_sec", 15)), step=5,
                help="Gibt an, wie oft das Live-Dashboard neue Daten von der DB abruft."
            )

    st.markdown("---")
    col_save, col_reset = st.columns([1, 1])
    with col_save:
        if st.button("💾 Einstellungen als neuen Standard in config.json speichern", type="primary", width="stretch"):
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            st.success("✅ Einstellungen dauerhaft in config.json gespeichert!")

    with col_reset:
        if st.button("↩️ Auf Werkseinstellungen zurücksetzen", width="stretch"):
            st.session_state.cfg = DEFAULT_CONFIG.copy()
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            st.info("Werkseinstellungen wiederhergestellt.")
            st.rerun()
