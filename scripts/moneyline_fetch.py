"""Pull a small, budget-capped slice of real MoneyLine market data each day.

Free tier = 1,000 credits/month =~ 33/day. This script is hard-capped well
under that so a bad day (lots of close games) can't blow the monthly budget:
  - /v1/edge/ev + /v1/edge/value, no league filter (1 credit each)   = 2 credits
  - up to MAX_GAMES of today's best model picks get real odds + real
    player props: 1 events-lookup call per (league,date) touched (1 credit,
    shared across every game in that league/day) + /v1/events/{id}/odds (3)
    + /v1/events/{id}/player-props (4) per game
On a typical day (3 games spread across 2-3 leagues) that's ~23-26 credits.
A BUDGET ceiling stops paid calls outright if something runs away.

Everything else on the dashboard keeps using the model's own numbers — this
only covers the handful of games that get a real sportsbook line each day.

usage: python3 -I moneyline_fetch.py <picks.json> <out.json>
env:   MONEYLINE_API_KEY (required)
"""
import sys, os, re, json, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

API = 'https://mlapi.bet'
KEY = os.environ.get('MONEYLINE_API_KEY')
if not KEY:
    print('MONEYLINE_API_KEY not set', file=sys.stderr)
    sys.exit(1)

PICKS, OUT = sys.argv[1], sys.argv[2]
MAX_GAMES = 3
BUDGET = 35  # hard credit ceiling for this run; stop paid calls once hit

LG_MAP = {'nfl': 'nfl', 'cfb': 'ncaa_football', 'nba': 'nba', 'cbb': 'ncaa_basketball', 'mlb': 'mlb'}

# Standard pro-league abbreviations -> a unique mascot word, used to match our
# picks.json abbreviations against MoneyLine's full team names (e.g. "Los
# Angeles Clippers"). Mascots are unique within a league even when cities
# aren't (LA, NY, Chicago each host two teams), so they're a safer match key
# than city names.
NBA_NAMES = {
    'ATL': 'Hawks', 'BKN': 'Nets', 'BOS': 'Celtics', 'CHA': 'Hornets', 'CHI': 'Bulls',
    'CLE': 'Cavaliers', 'DAL': 'Mavericks', 'DEN': 'Nuggets', 'DET': 'Pistons', 'GS': 'Warriors',
    'HOU': 'Rockets', 'IND': 'Pacers', 'LAC': 'Clippers', 'LAL': 'Lakers', 'MEM': 'Grizzlies',
    'MIA': 'Heat', 'MIL': 'Bucks', 'MIN': 'Timberwolves', 'NO': 'Pelicans', 'NY': 'Knicks',
    'OKC': 'Thunder', 'ORL': 'Magic', 'PHI': '76ers', 'PHX': 'Suns', 'POR': 'Blazers',
    'SA': 'Spurs', 'SAC': 'Kings', 'TOR': 'Raptors', 'UTAH': 'Jazz', 'WSH': 'Wizards',
}
MLB_NAMES = {
    'ARI': 'Diamondbacks', 'ATL': 'Braves', 'BAL': 'Orioles', 'BOS': 'Red Sox', 'CHC': 'Cubs',
    'CHW': 'White Sox', 'CIN': 'Reds', 'CLE': 'Guardians', 'COL': 'Rockies', 'DET': 'Tigers',
    'HOU': 'Astros', 'KC': 'Royals', 'LAA': 'Angels', 'LAD': 'Dodgers', 'MIA': 'Marlins',
    'MIL': 'Brewers', 'MIN': 'Twins', 'NYM': 'Mets', 'NYY': 'Yankees', 'OAK': 'Athletics',
    'ATH': 'Athletics', 'PHI': 'Phillies', 'PIT': 'Pirates', 'SD': 'Padres', 'SEA': 'Mariners',
    'SF': 'Giants', 'STL': 'Cardinals', 'TB': 'Rays', 'TEX': 'Rangers', 'TOR': 'Blue Jays',
    'WSH': 'Nationals',
}

credits_spent = 0


def call(path, cost, **params):
    """A metered call. Raises if it would exceed BUDGET, so callers can decide
    whether to skip rather than crash the whole run."""
    global credits_spent
    if credits_spent + cost > BUDGET:
        raise RuntimeError(f'budget ceiling ({BUDGET}cr) would be exceeded by {path}')
    q = '&'.join(f'{k}={urllib.parse.quote(str(v))}' for k, v in params.items() if v is not None)
    url = f'{API}{path}' + (f'?{q}' if q else '')
    req = urllib.request.Request(url, headers={'x-api-key': KEY, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.loads(r.read())
    credits_spent += cost
    return body


def norm_abbr(s):
    return (s or '').strip().upper()


def pick_games():
    """The day's best model picks across every league, most-confident first."""
    d = json.load(open(PICKS))
    cands = []
    for lg, mlg in LG_MAP.items():
        for g in d.get('leagues', {}).get(lg, {}).get('games', []):
            edge = abs((g.get('win') or 50) - (g.get('vegas') or 50))
            cands.append((1 if g.get('best') is True else 0, edge, lg, mlg, g))
    cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return cands[:MAX_GAMES]


_cbb_names = None


def cbb_name_for(abbr):
    """ESPN cbb abbreviation -> full school name, read from the school's own
    cbb_ranks.json (already in the repo, already has this exact crosswalk —
    no need to pay for /v1/teams, which came back empty for this league)."""
    global _cbb_names
    if _cbb_names is None:
        _cbb_names = {}
        try:
            path = os.path.join(os.path.dirname(PICKS) or '.', 'cbb_ranks.json')
            d = json.load(open(path))
            for t in (d.get('teams') or {}).values():
                ab = norm_abbr(t.get('abbr'))
                if ab:
                    _cbb_names[ab] = t.get('team') or ''
        except Exception as e:
            print(f'cbb_ranks.json load failed: {e}', file=sys.stderr)
    return _cbb_names.get(norm_abbr(abbr), abbr)


def name_for(lg, abbr_or_name):
    """Resolve a picks.json away/home field to a name that should appear
    inside MoneyLine's awayTeamName/homeTeamName. nfl/cfb already store a
    usable name (mascot or school); nba/mlb only store ESPN abbreviations,
    so those go through a static mascot map; cbb abbreviations go through
    the repo's own cbb_ranks.json crosswalk."""
    if lg == 'cbb':
        return cbb_name_for(abbr_or_name)
    if lg == 'nba':
        return NBA_NAMES.get(norm_abbr(abbr_or_name), abbr_or_name)
    if lg == 'mlb':
        return MLB_NAMES.get(norm_abbr(abbr_or_name), abbr_or_name)
    return abbr_or_name


def normalize(s):
    s = (s or '').lower()
    s = s.replace('&', ' and ')
    s = re.sub(r'\bumass\b', 'massachusetts', s)
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


_events_cache = {}


def events_for(mlg, date):
    k = (mlg, date)
    if k not in _events_cache:
        try:
            d = call('/v1/events', 1, league=mlg, date=date)
            _events_cache[k] = d.get('data') or []
        except Exception as e:
            print(f'events lookup failed {k}: {e}', file=sys.stderr)
            _events_cache[k] = []
    return _events_cache[k]


def _contains(short, full):
    s, f = normalize(short), normalize(full)
    return bool(s) and bool(f) and s in f


def match_event(evlist, away_name, home_name):
    """Match on team NAMES (not MoneyLine teamIds — /v1/teams came back
    empty for ncaa_basketball, and event-level awayTeamName/homeTeamName are
    already present in the /v1/events response we're paying for anyway)."""
    if not away_name or not home_name:
        return None
    for e in evlist:
        if _contains(away_name, e.get('awayTeamName', '')) and _contains(home_name, e.get('homeTeamName', '')):
            return e
    # home/away can come in swapped from upstream
    for e in evlist:
        if _contains(away_name, e.get('homeTeamName', '')) and _contains(home_name, e.get('awayTeamName', '')):
            return e
    return None


def main():
    out = {'updated': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
           'games': [], 'edge': {}, 'budget': BUDGET}

    for key, path in (('ev', '/v1/edge/ev'), ('value', '/v1/edge/value')):
        try:
            d = call(path, 1, limit=25)
            out['edge'][key] = d.get('data') or []
        except Exception as e:
            print(f'{path} failed: {e}', file=sys.stderr)

    for best, edge, lg, mlg, g in pick_games():
        date = g.get('date')
        rec = {'lg': lg, 'away': g.get('away'), 'home': g.get('home'), 'date': date, 'matched': False}
        away_name = name_for(lg, g.get('away'))
        home_name = name_for(lg, g.get('home'))
        evlist = events_for(mlg, date)
        ev = match_event(evlist, away_name, home_name)
        if not ev:
            # the game can fall on the next UTC calendar day for late local kickoffs
            try:
                nd = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
                ev = match_event(events_for(mlg, nd), away_name, home_name)
            except Exception:
                pass
        if ev:
            eid = ev.get('eventId')
            rec.update(eventId=eid, matched=True)
            try:
                rec['odds'] = call(f'/v1/events/{eid}/odds', 3).get('data')
            except Exception as e:
                print(f'odds failed {eid}: {e}', file=sys.stderr)
            try:
                rec['props'] = call(f'/v1/events/{eid}/player-props', 4).get('data')
            except Exception as e:
                print(f'props failed {eid}: {e}', file=sys.stderr)
        out['games'].append(rec)

    out['creditsSpent'] = credits_spent
    json.dump(out, open(OUT, 'w'), ensure_ascii=False)
    matched = sum(1 for g in out['games'] if g['matched'])
    print(f"done: ~{credits_spent} credits, {matched}/{len(out['games'])} picks matched, "
          f"edge.ev={len(out['edge'].get('ev', []))} edge.value={len(out['edge'].get('value', []))}")


if __name__ == '__main__':
    main()
