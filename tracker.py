import csv
import math
import os
from datetime import datetime, timezone, timedelta
import requests
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

LEAGUE_ID = os.environ["LEAGUE_ID"]
TEAM_ID = int(os.environ["TEAM_ID"])
SWID = os.environ.get("SWID", "")
ESPN_S2 = os.environ.get("ESPN_S2", "")
SEASON_ID = 2026
CSV_FILE = "win_prob_log.csv"
CHART_FILE = "win_prob_chart.png"

URL = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{SEASON_ID}/segments/0/leagues/{LEAGUE_ID}"
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
PARAMS = {"view": ["mMatchupScore", "mScoreboard", "mBoxscore"]}
COOKIES = {"SWID": SWID, "espn_s2": ESPN_S2} if SWID else {}

def games_are_live():
    try:
        sb = requests.get(SCOREBOARD_URL, timeout=15).json()
        for event in sb.get("events", []):
            if event.get("status", {}).get("type", {}).get("state", "") == "in":
                return True
    except Exception:
        return True
    return False

def estimate_win_prob(my_proj, opp_proj, my_live, opp_live):
    my_rem = max(0.0, my_proj - my_live)
    opp_rem = max(0.0, opp_proj - opp_live)
    if my_rem == 0 and opp_rem == 0:
        return 100.0 if my_live > opp_live else (0.0 if my_live < opp_live else 50.0)
    # Calibrated to match ESPN's wider fantasy player standard deviation
    combined_std = max(2.5, math.sqrt((my_rem + opp_rem) * 5.2))
    z_score = (my_proj - opp_proj) / combined_std
    return round(0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0))) * 100, 1)

def main():
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    resp = requests.get(URL, params=PARAMS, cookies=COOKIES, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    current_week = data["scoringPeriodId"]
    for matchup in data.get("schedule", []):
        if matchup.get("matchupPeriodId") != current_week:
            continue
        home, away = matchup.get("home", {}), matchup.get("away", {})
        if TEAM_ID not in (home.get("teamId"), away.get("teamId")):
            continue

        me = home if home.get("teamId") == TEAM_ID else away
        opp = away if me is home else home

        my_live = round(me.get("totalPointsLive", me.get("totalPoints", 0.0)), 2)
        opp_live = round(opp.get("totalPointsLive", opp.get("totalPoints", 0.0)), 2)
        my_proj = round(me.get("totalProjectedPointsLive", my_live), 2)
        opp_proj = round(opp.get("totalProjectedPointsLive", opp_live), 2)

        # 1. Try ESPN's native winProbability field first (stored as 0.0 - 1.0 or 0 - 100)
        espn_wp = me.get("winProbability")
        if espn_wp is not None:
            win_pct = round(espn_wp * 100 if espn_wp <= 1.0 else espn_wp, 1)
        else:
            win_pct = estimate_win_prob(my_proj, opp_proj, my_live, opp_live)

        live_now = games_are_live()
        already_logged_same = False
        if os.path.isfile(CSV_FILE):
            existing = pd.read_csv(CSV_FILE)
            wk_existing = existing[existing["week"] == current_week]
            if not wk_existing.empty:
                last = wk_existing.iloc[-1]
                if (last["my_live"] == my_live and last["opp_live"] == opp_live
                        and last["win_pct"] == win_pct):
                    already_logged_same = True

        if not is_manual and not live_now and already_logged_same:
            print("No live NFL games and no score change. Skipping log.")
            return

        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        file_exists = os.path.isfile(CSV_FILE)
        with open(CSV_FILE, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "week", "my_live", "opp_live", "my_proj", "opp_proj", "win_pct"])
            writer.writerow([now, current_week, my_live, opp_live, my_proj, opp_proj, win_pct])
        break

    if os.path.isfile(CSV_FILE):
        df = pd.read_csv(CSV_FILE)
        latest_week = df["week"].iloc[-1]
        wk = df[df["week"] == latest_week].copy()
        wk["timestamp"] = pd.to_datetime(wk["timestamp"])

        fig, ax = plt.subplots(figsize=(10, 4.5))
        ax.plot(wk["timestamp"], wk["win_pct"], linewidth=2.5, color="#1f77b4", marker="o", markersize=4)
        ax.axhline(50, color="gray", linestyle="--", alpha=0.6)
        ax.fill_between(wk["timestamp"], wk["win_pct"], 50, where=(wk["win_pct"] >= 50), color="#2ca02c", alpha=0.15)
        ax.fill_between(wk["timestamp"], wk["win_pct"], 50, where=(wk["win_pct"] < 50), color="#d62728", alpha=0.15)

        ax.set_ylim(0, 100)
        ax.set_ylabel("Win Probability (%)")
        ax.set_title(f"Week {latest_week} Matchup Win Probability (Latest: {wk['win_pct'].iloc[-1]}% | Score: {wk['my_live'].iloc[-1]}-{wk['opp_live'].iloc[-1]})")
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%a %H:%M"))
        plt.xticks(rotation=25)
        plt.tight_layout()
        plt.savefig(CHART_FILE, dpi=150)

if __name__ == "__main__":
    main()
