import os
import re
import json
import time
import unicodedata
from datetime import datetime, timezone, timedelta
import requests

# ---------- Config ----------
API_KEY     = os.environ.get("ODDS_API_KEY", "")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

STATE_FILE  = "ufc_seen.json"
ODDS_URL    = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts/odds"
BOOK        = "betonlineag"
ESPN_URL    = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
WINDOW_DAYS = 45

# Add last names here to silence fights involving them (e.g. "ditcheva", "stots").
# Muted fights are marked seen silently and never alert.
MUTE_LAST_NAMES = set()


# ---------- Odds side ----------
def fetch_mma_odds():
    if not API_KEY:
        raise RuntimeError("No Odds API key found — check the ODDS_API_KEY secret.")
    params = {"apiKey": API_KEY, "regions": "us",
              "markets": "h2h", "oddsFormat": "american"}
    r = requests.get(ODDS_URL, params=params, timeout=20)
    r.raise_for_status()
    print("Odds API requests remaining this month:", r.headers.get("x-requests-remaining"))
    return r.json()


def betonline_line(event):
    for bk in event.get("bookmakers", []):
        if bk.get("key") != BOOK:
            continue
        for m in bk.get("markets", []):
            if m.get("key") == "h2h" and len(m.get("outcomes", [])) >= 2:
                return {o["name"]: o["price"] for o in m["outcomes"]}
    return None


# ---------- Name matching ----------
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

def last_name_key(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[.'\-]", " ", n.lower())
    toks = [t for t in n.split() if t and t not in SUFFIXES]
    return toks[-1] if toks else ""

def bout_key(a, b):
    return frozenset({last_name_key(a), last_name_key(b)})


# ---------- ESPN: confirmed UFC bouts (label only, never a filter) ----------
def load_ufc_bouts():
    try:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=WINDOW_DAYS)
        cal = requests.get(ESPN_URL, timeout=20).json().get("leagues", [{}])[0].get("calendar", [])

        date_strs = set()
        for item in cal:
            try:
                s = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
            except Exception:
                continue
            if now - timedelta(days=2) <= s <= cutoff:
                date_strs.add(s.strftime("%Y%m%d"))
                date_strs.add((s - timedelta(hours=5)).strftime("%Y%m%d"))

        bouts, seen_ev = set(), set()
        for d in sorted(date_strs):
            try:
                evs = requests.get(ESPN_URL, params={"dates": d}, timeout=20).json().get("events", [])
            except Exception:
                continue
            for ev in evs:
                if ev.get("id") in seen_ev:
                    continue
                seen_ev.add(ev.get("id"))
                for comp in ev.get("competitions", []):
                    nm = [c.get("athlete", {}).get("fullName", "")
                          for c in comp.get("competitors", []) if c.get("athlete")]
                    nm = [x for x in nm if x]
                    if len(nm) == 2:
                        bouts.add(bout_key(nm[0], nm[1]))
        print(f"ESPN: {len(bouts)} confirmed UFC bouts loaded (labelling only).")
        return bouts
    except Exception as e:
        # ESPN failing must never stop alerts — we just lose the 🥊 label.
        print("ESPN lookup failed, continuing without labels:", e)
        return set()


# ---------- State + Discord ----------
def load_seen():
    if not os.path.exists(STATE_FILE):
        return set(), True
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f).get("seen_ids", [])), False
    except Exception:
        return set(), True

def save_seen(seen_ids):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen_ids": sorted(seen_ids)}, f, indent=2)

def send_discord(message):
    if not WEBHOOK_URL:
        raise RuntimeError("No Discord webhook found — check the DISCORD_WEBHOOK_URL secret.")
    resp = requests.post(WEBHOOK_URL, json={"content": message}, timeout=20)
    resp.raise_for_status()

def fmt_time(iso_str):
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%a %b %d, %Y")
    except Exception:
        return iso_str


# ---------- Main ----------
def main():
    events = fetch_mma_odds()
    seen, first_run = load_seen()
    ufc_bouts = load_ufc_bouts()

    priced = {}
    for ev in events:
        line = betonline_line(ev)
        if line is None:
            continue
        confirmed = bout_key(ev["home_team"], ev["away_team"]) in ufc_bouts
        priced[ev["id"]] = (ev, line, confirmed)

    n_conf = sum(1 for _, _, c in priced.values() if c)
    print(f"BetOnline pricing {len(priced)} fights ({n_conf} ESPN-confirmed UFC).")

    if first_run:
        seen = set(priced.keys())
        save_seen(seen)
        send_discord(f"🔧 UFC line bot started. Seeded {len(seen)} fights already on the "
                     f"board — alerts begin with the next new opener.")
        print(f"First run: seeded {len(seen)} fights, no alerts sent.")
        return

    new_ids = [eid for eid in priced if eid not in seen]
    print(f"{len(new_ids)} new opener(s).")

    sent = 0
    for eid in new_ids:
        ev, line, confirmed = priced[eid]

        names = [ev["away_team"], ev["home_team"]]
        if any(last_name_key(n) in MUTE_LAST_NAMES for n in names):
            seen.add(eid)
            print("Muted:", names[0], "vs", names[1])
            continue

        odds_str = "   |   ".join(f"{n}: {int(p):+d}" for n, p in line.items())
        header = ("🥊 **New BetOnline line — UFC confirmed**" if confirmed
                  else "🆕 **New BetOnline MMA line**")
        send_discord(f"{header}\n{names[0]} vs {names[1]}\n"
                     f"{fmt_time(ev['commence_time'])}\n{odds_str}")
        seen.add(eid)
        sent += 1
        print(("Alerted 🥊:" if confirmed else "Alerted 🆕:"), names[0], "vs", names[1])
        time.sleep(1)   # stay under Discord's webhook rate limit

    save_seen(seen)
    print(f"Done. {sent} alert(s) sent.")


main()
