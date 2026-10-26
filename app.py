import datetime
import json
import os
import re
from typing import List, Tuple, Dict, Any
import requests
import streamlit as st
import pandas as pd
import altair as alt
import streamlit.components.v1 as components

# ---------------------------------------------------------
# SEITEN-KONFIGURATION
# ---------------------------------------------------------
st.set_page_config(page_title="BÜ Steingleis Monitor", page_icon="🚦", layout="wide")

CONFIG_FILE = "config.json"
STATION_NAME = "Neustadt(Weinstr)Hbf"
API_BASE = f"https://dbf.finalrewind.org/{STATION_NAME}.json?version=3"

DEFAULT_CONFIG = {
    "modus": "🔴 Live-Echtzeit (Jetzt)",
    "auto_refresh": True,
    "refresh_interval_sec": 15,
    "sound_alarm": True,
    "sound_warn_sec": 45,
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

cfg = load_config()

WEST_CORRIDOR_KEYWORDS = [
    "lambrecht", "neidenfels", "weidenthal", "frankenstein", "hochspeyer",
    "kaiserslautern", "landstuhl", "homburg", "saarbrücken", "saarbruecken",
    "st. ingbert", "saarlouis", "merzig", "trier", "koblenz", "paris",
    "kusel", "pirmasens"
]

if hasattr(st, "fragment"):
    fragment_dec = st.fragment
elif hasattr(st, "experimental_fragment"):
    fragment_dec = st.experimental_fragment
else:
    def fragment_dec(*args, **kwargs):
        def wrapper(f):
            return f
        return wrapper

def check_corridor_match(item) -> bool:
    if not item:
        return False
    if isinstance(item, str):
        target = item.lower()
        return any(kw in target for kw in WEST_CORRIDOR_KEYWORDS)
    if isinstance(item, list):
        return any(check_corridor_match(x) for x in item)
    if isinstance(item, dict):
        return any(check_corridor_match(v) for v in item.values())
    return False

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
    ref = reference_dt or datetime.datetime.now()
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

def fetch_dbf_board(mode: str = "dep", use_realtime: bool = True) -> Tuple[List[Dict[str, Any]], str]:
    headers = {"User-Agent": "Streamlit-Steingleis-Monitor"}
    url = f"{API_BASE}&mode={mode}" if mode == "arr" else API_BASE
    if not use_realtime:
        url += "&rt=0"

    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data and data["error"]:
            return [], f"API: {data['error']}"
        return data.get("departures", []), ""
    except Exception as e:
        return [], f"Verbindungsfehler: {e}"

def get_steingleis_events(lead_dep: int, lag_dep: int, lead_arr: int, lag_arr: int, 
                          use_realtime: bool, reference_dt: datetime.datetime):
    raw_intervals = []
    zugliste = []
    seen_events = set()

    dep_data, _ = fetch_dbf_board(mode="dep", use_realtime=use_realtime)
    arr_data, _ = fetch_dbf_board(mode="arr", use_realtime=use_realtime)

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

        if check_corridor_match(dest) or check_corridor_match(post_stops):
            train_name = tr.get("train") or tr.get("line") or "Zug"
            sched_time = tr.get("scheduledDeparture") or tr.get("departure") or tr.get("time")
            delay = tr.get("delayDeparture", 0) if use_realtime else 0

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
                        "Richtung": "➡️ Abfahrt (West)",
                        "Zug": train_name,
                        "Start/Ziel": dest,
                        "Fahrplanzeit": dep_dt.strftime("%H:%M") + delay_str,
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

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

        if check_corridor_match(origin) or check_corridor_match(pre_stops):
            train_name = tr.get("train") or tr.get("line") or "Zug"
            sched_time = tr.get("scheduledArrival") or tr.get("arrival") or tr.get("time")
            delay = tr.get("delayArrival", 0) if use_realtime else 0

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
                        "Richtung": "⬅️ Ankunft (West)",
                        "Zug": train_name,
                        "Start/Ziel": herkunft,
                        "Fahrplanzeit": arr_dt.strftime("%H:%M") + delay_str,
                        "Schließung": f"{start.strftime('%H:%M:%S')} - {end.strftime('%H:%M:%S')}",
                        "_start_dt": start
                    })

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
    """Erzeugt lückenlose Zeitblöcke für das 60-Minuten-Gantt-Diagramm."""
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

        # Offenes Intervall vor der Schließung
        if eff_start > curr:
            events.append({
                "Start": curr,
                "Ende": eff_start,
                "Status": "Freie Fahrt (Offen)",
                "Details": "BÜ geöffnet"
            })

        # Schließungs-Intervall
        events.append({
            "Start": eff_start,
            "Ende": eff_end,
            "Status": "Schranke Geschlossen",
            "Details": " + ".join(trains)
        })
        curr = max(curr, eff_end)

    # Restliches Zeitfenster nach der letzten Schließung
    if curr < window_end:
        events.append({
            "Start": curr,
            "Ende": window_end,
            "Status": "Freie Fahrt (Offen)",
            "Details": "BÜ geöffnet"
        })

    return pd.DataFrame(events)

# ---------------------------------------------------------
# BENUTZEROBERFLÄCHE
# ---------------------------------------------------------
st.title("🚦 BÜ Steingleis (Neustadt/Weinstraße)")

with st.sidebar:
    st.header("⚙️ Modus & Zeit")
    modus_options = ["🔴 Live-Echtzeit (Jetzt)", "📅 Fahrplan-Analyse (Wunschzeit)"]
    default_modus_idx = 0 if cfg.get("modus") == modus_options[0] else 1

    modus = st.radio("Betriebsmodus:", modus_options, index=default_modus_idx)
    now_real = datetime.datetime.now()

    if modus == modus_options[0]:
        use_realtime = True
        auto_refresh = st.toggle(
            f"🔄 Auto-Refresh ({cfg.get('refresh_interval_sec', 15)}s)", 
            value=cfg.get("auto_refresh", True)
        )
        plan_selected_time = None
    else:
        use_realtime = False
        auto_refresh = False
        st.caption("Echtzeit-Verspätungen deaktiviert (reiner Soll-Fahrplan).")
        plan_selected_time = st.time_input("Wunsch-Uhrzeit:", value=now_real.time())

    st.markdown("---")
    st.header("🔔 Akustischer Signalton")
    sound_alarm = st.toggle("Schrankengong aktivieren", value=cfg.get("sound_alarm", True))
    sound_warn_sec = st.slider("Vorwarnzeit (Sek. vor Schließung)", 15, 120, int(cfg.get("sound_warn_sec", 45)), step=5)

    if st.button("🔊 Signalton testen", use_container_width=True):
        components.html("<script>if(window.parent.playBUEBell) { window.parent.playBUEBell(3.0); }</script>", height=0)

    st.markdown("---")
    st.header("🛠️ Stellwerks-Parameter")
    lead_dep = st.slider("Schließung vor Abfahrt (Sek.)", 30, 360, int(cfg.get("lead_dep", 240)), step=10)
    lag_dep = st.slider("Öffnung nach Abfahrt (Sek.)", 30, 180, int(cfg.get("lag_dep", 90)), step=10)
    lead_arr = st.slider("Schließung vor Ankunft (Sek.)", 60, 240, int(cfg.get("lead_arr", 160)), step=10)
    lag_arr = st.slider("Öffnung vor Ankunft (Sek.)", 10, 90, int(cfg.get("lag_arr", 40)), step=5)
    min_gap = st.slider("Mindestöffnungslücke (Sek.)", 0, 120, int(cfg.get("min_gap", 30)), step=10)

    st.markdown("---")
    # 0.) JSON DUMP: EINSTELLUNGEN DAUERHAFT SICHERN
    if st.button("💾 Einstellungen als Standard speichern", use_container_width=True, type="primary"):
        new_config = {
            "modus": modus,
            "auto_refresh": auto_refresh,
            "refresh_interval_sec": int(cfg.get("refresh_interval_sec", 15)),
            "sound_alarm": sound_alarm,
            "sound_warn_sec": sound_warn_sec,
            "lead_dep": int(lead_dep),
            "lag_dep": int(lag_dep),
            "lead_arr": int(lead_arr),
            "lag_arr": int(lag_arr),
            "min_gap": int(min_gap)
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2)
        st.success("Werte in config.json gespeichert!")

# ---------------------------------------------------------
# SOUND-ENGINE & LIVE-UHR (CLIENTSEITIG IN JAVASCRIPT)
# ---------------------------------------------------------
components.html(
    """
    <script>
    const parentDoc = window.parent.document;

    // 1. Authentischer elektronischer BÜ-Wecker / Gong (Web Audio API)
    window.parent.playBUEBell = function(durationSec = 2.5) {
        try {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            if (!AudioContext) return;
            const ctx = new AudioContext();
            const strikeInterval = 0.55; // Gong-Schlag alle 550ms
            const strikes = Math.floor(durationSec / strikeInterval);

            for (let i = 0; i < strikes; i++) {
                const strikeTime = ctx.currentTime + (i * strikeInterval);
                // Harmonische Obertöne für authentischen BÜ-Klang (Gong/Schale)
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
        } catch(e) {
            console.log("AudioContext noch nicht durch Nutzerklick aktiviert.", e);
        }
    };

    // 2. Kontinuierliche Live-Uhr unten links
    let clockBadge = parentDoc.getElementById('steingleis-live-clock');
    if (!clockBadge) {
        clockBadge = parentDoc.createElement('div');
        clockBadge.id = 'steingleis-live-clock';
        clockBadge.style.position = 'fixed';
        clockBadge.style.bottom = '16px';
        clockBadge.style.left = '16px';
        clockBadge.style.backgroundColor = 'rgba(15, 23, 42, 0.85)';
        clockBadge.style.color = '#38bdf8';
        clockBadge.style.padding = '6px 12px';
        clockBadge.style.borderRadius = '8px';
        clockBadge.style.fontFamily = 'ui-monospace, monospace';
        clockBadge.style.fontSize = '13px';
        clockBadge.style.fontWeight = 'bold';
        clockBadge.style.zIndex = '999999';
        clockBadge.style.boxShadow = '0 4px 12px rgba(0,0,0,0.3)';
        clockBadge.style.border = '1px solid rgba(255,255,255,0.15)';
        clockBadge.style.backdropFilter = 'blur(6px)';
        clockBadge.style.pointerEvents = 'none';
        clockBadge.innerHTML = '🕒 <span id="steingleis-clock-text">--:--:--</span>';
        parentDoc.body.appendChild(clockBadge);
    }

    function tickClock() {
        const el = parentDoc.getElementById('steingleis-clock-text');
        if (el) {
            el.innerText = new Date().toLocaleTimeString('de-DE');
        }
    }
    if (!window.parent.steingleisClockInterval) {
        window.parent.steingleisClockInterval = setInterval(tickClock, 1000);
    }
    tickClock();
    </script>
    """,
    height=0
)

refresh_interval = int(cfg.get("refresh_interval_sec", 15)) if (use_realtime and auto_refresh) else None

# ---------------------------------------------------------
# DYNAMISCHES DASHBOARD (FRAGMENT)
# ---------------------------------------------------------
@fragment_dec(run_every=refresh_interval)
def render_live_dashboard():
    current_time_tick = datetime.datetime.now()
    if use_realtime:
        calc_base_dt = current_time_tick
    else:
        calc_base_dt = current_time_tick.replace(
            hour=plan_selected_time.hour, 
            minute=plan_selected_time.minute, 
            second=0
        )

    raw_intervals, zugliste = get_steingleis_events(
        lead_dep, lag_dep, lead_arr, lag_arr, use_realtime, calc_base_dt
    )
    merged = merge_intervals(raw_intervals, min_gap)

    is_closed = False
    current_end = None
    next_closure = None
    active_reasons = []

    for start, end, trains in merged:
        if start <= calc_base_dt <= end:
            is_closed = True
            current_end = end
            active_reasons = trains
            break
        elif start > calc_base_dt:
            if next_closure is None or start < next_closure[0]:
                next_closure = (start, end, trains)

    # 3.) AKUSTISCHER ALARM AUSLÖSEN (falls kurz vor oder während Schließbeginn)
    if use_realtime and sound_alarm:
        should_alarm = False
        if next_closure:
            sec_to_next = (next_closure[0] - calc_base_dt).total_seconds()
            if 0 < sec_to_next <= sound_warn_sec:
                should_alarm = True
        elif is_closed:
            # Während der Schließphase kurz anschlagen
            sec_since_start = (calc_base_dt - start).total_seconds() if 'start' in locals() else 0
            if 0 <= sec_since_start <= 15:
                should_alarm = True

        if should_alarm:
            components.html("<script>if(window.parent.playBUEBell){ window.parent.playBUEBell(2.5); }</script>", height=0)

    # Status-Kacheln
    col1, col2 = st.columns([1, 1])

    with col1:
        sub_title = (
            f"Live-Status (Stand: {current_time_tick.strftime('%H:%M:%S')} Uhr)" 
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
            st.dataframe(pd.DataFrame(future_rows), use_container_width=True, hide_index=True)
        else:
            st.write("Keine Schließungen im gewählten Zeitabschnitt.")

    # 2.) VISUELLER ZEITSTRAHL (GANTT FÜR DIE NÄCHSTEN 60 MINUTEN)
    st.markdown("---")
    st.subheader("⏱️ Zeitstrahl nächste 60 Minuten")
    df_timeline = build_timeline_dataframe(calc_base_dt, merged, duration_min=60)
    
    if not df_timeline.empty:
        chart = (
            alt.Chart(df_timeline)
            .mark_bar(cornerRadius=4, height=32)
            .encode(
                x=alt.X('Start:T', title='Uhrzeit', axis=alt.Axis(format='%H:%M', grid=True)),
                x2='Ende:T',
                color=alt.Color(
                    'Status:N',
                    scale=alt.Scale(
                        domain=['Freie Fahrt (Offen)', 'Schranke Geschlossen'],
                        range=['#22c55e', '#ef4444']
                    ),
                    legend=alt.Legend(title=None, orient='top')
                ),
                tooltip=[
                    alt.Tooltip('Status:N', title='Status'),
                    alt.Tooltip('Start:T', format='%H:%M:%S', title='Von'),
                    alt.Tooltip('Ende:T', format='%H:%M:%S', title='Bis'),
                    alt.Tooltip('Details:N', title='Details / Züge')
                ]
            )
            .properties(height=80)
        )
        st.altair_chart(chart, use_container_width=True)

    # Detail-Tabelle aller Züge
    with st.expander(f"🔍 Alle erfassten Züge am Steingleis ({len(zugliste)} Fahrten)"):
        if zugliste:
            df_display = pd.DataFrame(zugliste).drop(columns=["_start_dt"])
            st.dataframe(df_display, use_container_width=True, hide_index=True)
        else:
            st.write("Keine Fahrten gefunden.")

render_live_dashboard()