import csv
import json
import math
import os
from datetime import datetime, timezone, timedelta
import requests

TEAMS_FILE = "teams.json"

SPORT_CONFIG = {
    "ffl": {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"},
    "fba": {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"},
    "fhl": {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"},
    "flb": {"scoreboard": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"},
}

def fallback_season(sport):
    """Fallback season year if scoreboard API is unreachable."""
    now = datetime.now(timezone.utc)
    # NFL season starts in Aug/Sep and runs through Feb of the next year (uses start year)
    if sport == "ffl":
        return now.year if now.month >= 7 else now.year - 1
    # NBA/NHL span two calendar years and ESPN indexes them by the ending year
    if sport in ("fba", "fhl"):
        return now.year + 1 if now.month >= 8 else now.year
    return now.year

def get_sport_status(sport):
    """Returns (is_live, active_season_year) directly from ESPN's live scoreboard."""
    url = SPORT_CONFIG.get(sport, {}).get("scoreboard")
    season = fallback_season(sport)
    if not url:
        return True, season
    try:
        sb = requests.get(url, timeout=15).json()
        leagues = sb.get("leagues", [])
        if leagues and leagues[0].get("season", {}).get("year"):
            season = int(leagues[0]["season"]["year"])
        for event in sb.get("events", []):
            if event.get("status", {}).get("type", {}).get("state", "") == "in":
                return True, season
        return False, season
    except Exception:
        return True, season

def estimate_win_prob(my_proj, opp_proj, my_live, opp_live):
    my_rem = max(0.0, my_proj - my_live)
    opp_rem = max(0.0, opp_proj - opp_live)
    if my_rem == 0 and opp_rem == 0:
        return 100.0 if my_live > opp_live else (0.0 if my_live < opp_live else 50.0)
    combined_std = max(2.5, math.sqrt((my_rem + opp_rem) * 5.2))
    z_score = (my_proj - opp_proj) / combined_std
    return round(0.5 * (1.0 + math.erf(z_score / math.sqrt(2.0))) * 100, 1)

def poll_team(team, is_manual, status_cache):
    sport = team.get("sport", "ffl")
    league_id = str(team.get("leagueId") or os.environ.get("LEAGUE_ID", ""))
    team_id = int(team.get("teamId") or os.environ.get("TEAM_ID", 1))
    csv_file = team.get("csvFile") or f"logs/{sport}_{league_id}_{team_id}.csv"

    if not league_id:
        return

    if sport not in status_cache:
        status_cache[sport] = get_sport_status(sport)
    live_now, detected_season = status_cache[sport]
    season_id = int(team.get("seasonId") or detected_season)

    # Fast exit if no games are live in this sport (unless triggered manually)
    # Still run one final pass if the last logged entry hasn't captured final post-game scores
    os.makedirs(os.path.dirname(csv_file) or ".", exist_ok=True)
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/{sport}/seasons/{season_id}/segments/0/leagues/{league_id}"
    cookies = {}
    if team.get("id") == "primary" and os.environ.get("SWID"):
        cookies = {"SWID": os.environ["SWID"], "espn_s2": os.environ.get("ESPN_S2", "")}

    resp = requests.get(url, params={"view": ["mMatchupScore", "mScoreboard", "mBoxscore"]}, cookies=cookies, timeout=20)
    if resp.status_code != 200:
        print(f"Skipping {team.get('name')} ({resp.status_code})")
        return
    data = resp.json()

    current_week = data.get("scoringPeriodId")
    for matchup in data.get("schedule", []):
        if matchup.get("matchupPeriodId") != current_week:
            continue
        home, away = matchup.get("home", {}), matchup.get("away", {})
        if team_id not in (home.get("teamId"), away.get("teamId")):
            continue

        me = home if home.get("teamId") == team_id else away
        opp = away if me is home else home

        my_live = round(me.get("totalPointsLive", me.get("totalPoints", 0.0)), 2)
        opp_live = round(opp.get("totalPointsLive", opp.get("totalPoints", 0.0)), 2)
        my_proj = round(me.get("totalProjectedPointsLive", my_live), 2)
        opp_proj = round(opp.get("totalProjectedPointsLive", opp_live), 2)

        espn_wp = me.get("winProbability")
        if espn_wp is not None:
            win_pct = round(espn_wp * 100 if espn_wp <= 1.0 else espn_wp, 1)
        else:
            win_pct = estimate_win_prob(my_proj, opp_proj, my_live, opp_live)

        already_logged_same = False
        if os.path.isfile(csv_file):
            with open(csv_file, "r") as f:
                rows = list(csv.reader(f))
                wk_rows = [r for r in rows[1:] if len(r) >= 7 and int(r[1]) == current_week]
                if wk_rows:
                    last = wk_rows[-1]
                    if float(last[2]) == my_live and float(last[3]) == opp_live and float(last[6]) == win_pct:
                        already_logged_same = True

        if not is_manual and not live_now and already_logged_same:
            print(f"[{sport}] No live games and score unchanged for {team.get('name')}. Skipping.")
            return

        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        file_exists = os.path.isfile(csv_file)
        with open(csv_file, "a", newline="") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(["timestamp", "week", "my_live", "opp_live", "my_proj", "opp_proj", "win_pct"])
            writer.writerow([now, current_week, my_live, opp_live, my_proj, opp_proj, win_pct])
        break

def main():
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    teams = []
    if os.path.isfile(TEAMS_FILE):
        with open(TEAMS_FILE, "r") as f:
            teams = json.load(f)
    if not teams:
        teams = [{"id": "primary", "name": "My Team", "sport": "ffl", "csvFile": "win_prob_log.csv"}]

    status_cache = {}
    for team in teams:
        poll_team(team, is_manual, status_cache)

if __name__ == "__main__":
    main()
