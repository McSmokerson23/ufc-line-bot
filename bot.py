import os
import re
import json
import time
import subprocess
import unicodedata
from datetime import datetime, timezone, timedelta
import requests

# ---------------- Config ----------------
API_KEY     = os.environ.get("ODDS_API_KEY", "")
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

BASE       = "https://api.the-odds-api.com/v4/sports/mma_mixed_martial_arts"
ESPN_URL   = "https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard"
BOOK       = "betonlineag"
STATE_FILE = "ufc_state.json"

POLL_SECONDS      = 120                                        # free /events poll interval
LOOP_MINUTES      = int(os.environ.get("LOOP_MINUTES", "300"))  # how long this run watches
SWEEP_HOURS       = 4                                          # backstop /odds sweep
MAX_ODDS_PER_DAY  = 14                                         # credit guard (500/mo cap)
WINDOW_DAYS       = 45
ALERT_UNPRICED    = True    # notify when a fight appears before BetOnline prices it
GIT_COMMIT        = os.environ.get("GIT_COMMIT", "1") == "1"

# Add last names to silence them, e.g. {"ditcheva", "stots"}
MUTE_LAST_NAMES = set()


# ---------------- Name matching ----------------
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

def last_name_key(name):
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[.'\-]", " ", n.lower())
    toks = [t for t in n.split() if t and t not in SUFFIXES]
    return toks[-1] if toks else ""

def bout_key(a, b):
    return frozenset({last_name_key(a), last_name_key(b)})


# ---------------- API calls ----------------
def fetch_events():
    """FREE — does not count against the usage quota."""
    r = requests.get(f"{BASE}/events", params={"apiKey": API_KEY}, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_odds():
    """Costs 1 credit."""
    r = requests.get(f"{BASE}/odds",
                     params={"apiKey": API_KEY, "regions": "us",
                             "markets": "h2h", "oddsFormat": "american"}, timeout=25)
    r.raise_for_status()
    log(f"odds sweep OK — credits remaining: {r.headers.get('x-requests-remaining')}")
    return r.json()

def betonline_line(event):
    for bk in event.get("bookmakers", []):
        if bk.get("key") != BOOK:
            continue
        for m in bk.get("markets", []):
            if m.get("key") == "h2h" and len(m.get("outcomes", [])) >= 2:
                return {o["name"]: o["price"] for o in m["outcomes"]}
    return None


# ---------------- ESPN labels (never blocks alerts) ----------------
def load_ufc_bouts():
    try:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=WINDOW_DAYS)
        cal = requests.get(ESPN_URL, timeout=25).json().get("leagues", [{}])[0].get("calendar", [])
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
                evs = requests.get(ESPN_URL, params={"dates": d}, timeout=25).json().get("events", [])
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
        log(f"ESPN: {len(bouts)} confirmed UFC bouts (labels only)")
        return bouts
    except Exception as e:
        log(f"ESPN lookup failed ({e}) — continuing without labels")
        return set()


# ---------------- State ----------------
def blank_state():
    return {"seen_events": [], "alerted": [], "announced": [],
            "last_sweep": None, "odds_calls": {}}

def load_state():
    if not os.path.exists(STATE_FILE):
        return blank_state(), True
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in blank_state().items():
            s.setdefault(k, v)
        return s, False
    except Exception:
        return blank_state(), True

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    if not GIT_COMMIT:
        return
    try:
        subprocess.run(["git", "config", "user.name", "ufc-line-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=False)
        subprocess.run(["git", "add", STATE_FILE], check=False)
        r = subprocess.run(["git", "diff", "--staged", "--quiet"])
        if r.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update bot state"], check=False)
            subprocess.run(["git", "pull", "--rebase", "-q"], check=False)
            subprocess.run(["git", "push", "-q"], check=False)
    except Exception as e:
        log(f"git save failed (non-fatal): {e}")


def credits_used_today(state):
    return state["odds_calls"].get(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0)

def record_odds_call(state):
    d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state["odds_calls"][d] = state["odds_calls"].get(d, 0) + 1
    for k in list(state["odds_calls"]):
        if k < (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d"):
            del state["odds_calls"][k]


# ---------------- Output ----------------
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)

def send_discord(message):
    try:
        requests.post(WEBHOOK_URL, json={"content": message}, timeout=20).raise_for_status()
        time.sleep(1)
    except Exception as e:
        log(f"Discord post failed: {e}")

def fmt_time(iso_str):
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%a %b %d, %Y")
    except Exception:
        return iso_str

def muted(a, b):
    return last_name_key(a) in MUTE_LAST_NAMES or last_name_key(b) in MUTE_LAST_NAMES


# ---------------- Sweep ----------------
def do_sweep(state, ufc_bouts, reason):
    if credits_used_today(state) >= MAX_ODDS_PER_DAY:
        log(f"sweep skipped ({reason}) — daily credit cap reached")
        return
    log(f"sweep triggered: {reason}")
    try:
        odds = fetch_odds()
    except Exception as e:
        log(f"odds fetch failed: {e}")
        return
    record_odds_call(state)
    state["last_sweep"] = datetime.now(timezone.utc).isoformat()

    alerted = set(state["alerted"])
    sent = 0
    for ev in odds:
        eid = ev.get("id")
        if not eid or eid in alerted:
            continue
        line = betonline_line(ev)
        if line is None:
            continue
        away, home = ev.get("away_team", "?"), ev.get("home_team", "?")
        if muted(away, home):
            alerted.add(eid)
            continue
        confirmed = bout_key(home, away) in ufc_bouts
        header = ("🥊 **New BetOnline line — UFC confirmed**" if confirmed
                  else "🆕 **New BetOnline MMA line**")
        odds_str = "   |   ".join(f"{n}: {int(p):+d}" for n, p in line.items())
        send_discord(f"{header}\n{away} vs {home}\n{fmt_time(ev.get('commence_time',''))}\n{odds_str}")
        alerted.add(eid)
        sent += 1
        log(f"ALERT {'UFC' if confirmed else 'MMA'}: {away} vs {home}")

    state["alerted"] = sorted(alerted)
    log(f"sweep done — {sent} alert(s), credits used today: {credits_used_today(state)}")


# ---------------- Main ----------------
def main():
    if not API_KEY or not WEBHOOK_URL:
        raise RuntimeError("Missing ODDS_API_KEY or DISCORD_WEBHOOK_URL secret.")

    state, fresh = load_state()
    ufc_bouts = load_ufc_bouts()
    espn_refreshed = datetime.now(timezone.utc)

    deadline = datetime.now(timezone.utc) + timedelta(minutes=LOOP_MINUTES)
    log(f"watching for {LOOP_MINUTES} min, polling /events every {POLL_SECONDS}s")

    if fresh:
        try:
            evs = fetch_events()
        except Exception as e:
            log(f"initial events fetch failed: {e}")
            return
        state["seen_events"] = sorted({e["id"] for e in evs})
        do_sweep(state, ufc_bouts, "initial seed")
        state["announced"] = list(state["seen_events"])
        save_state(state)
        send_discord(f"🔧 UFC line bot online — watching {len(state['seen_events'])} fights. "
                     f"Checking every {POLL_SECONDS // 60} min.")
        log(f"seeded {len(state['seen_events'])} events")

    while datetime.now(timezone.utc) < deadline:
        try:
            evs = fetch_events()
        except Exception as e:
            log(f"events poll failed: {e}")
            time.sleep(POLL_SECONDS)
            continue

        by_id = {e["id"]: e for e in evs if e.get("id")}
        seen = set(state["seen_events"])
        new_ids = [i for i in by_id if i not in seen]

        if datetime.now(timezone.utc) - espn_refreshed > timedelta(hours=1):
            ufc_bouts = load_ufc_bouts()
            espn_refreshed = datetime.now(timezone.utc)

        changed = False

        if new_ids:
            log(f"{len(new_ids)} NEW event(s) detected")
            if ALERT_UNPRICED:
                announced = set(state["announced"])
                for i in new_ids:
                    ev = by_id[i]
                    away, home = ev.get("away_team", "?"), ev.get("home_team", "?")
                    if i in announced or muted(away, home):
                        continue
                    confirmed = bout_key(home, away) in ufc_bouts
                    tag = "UFC" if confirmed else "MMA"
                    send_discord(f"📋 **New fight added ({tag})** — no BetOnline price yet\n"
                                 f"{away} vs {home}\n{fmt_time(ev.get('commence_time',''))}")
                    announced.add(i)
                    log(f"ANNOUNCED: {away} vs {home}")
                state["announced"] = sorted(announced)
            do_sweep(state, ufc_bouts, f"{len(new_ids)} new event(s)")
            state["seen_events"] = sorted(seen | set(by_id))
            changed = True
        else:
            last = state.get("last_sweep")
            due = True
            if last:
                try:
                    due = datetime.now(timezone.utc) - datetime.fromisoformat(last) > timedelta(hours=SWEEP_HOURS)
                except Exception:
                    due = True
            if due:
                do_sweep(state, ufc_bouts, f"{SWEEP_HOURS}h backstop")
                changed = True

        if changed:
            save_state(state)

        time.sleep(POLL_SECONDS)

    log("watch window ended")
    save_state(state)


main()
