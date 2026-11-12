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
import sys, os, json, urllib.request, urllib.parse
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


_teams_cache = {}


def teams_for(mlg):
    """abbreviation (upper) -> teamId, for one league. Cached per run."""
    if mlg not in _teams_cache:
        amap = {}
        try:
            d = call('/v1/teams', 1, league=mlg, limit=400)
            for t in (d.get('data') or []):
                ab = norm_abbr(t.get('abbreviation'))
                if ab:
                    amap[ab] = t.get('teamId')
        except Exception as e:
            print(f'teams lookup failed {mlg}: {e}', file=sys.stderr)
        _teams_cache[mlg] = amap
    return _teams_cache[mlg]


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


def match_event(evlist, away_id, home_id):
    """Match on MoneyLine team ids resolved from our abbreviations — far more
    reliable than fuzzy name matching, especially for college team names that
    different providers abbreviate differently."""
    if not away_id or not home_id:
        return None
    for e in evlist:
        if e.get('awayTeamId') == away_id and e.get('homeTeamId') == home_id:
            return e
    # some events list only one side populated, or home/away swapped upstream
    for e in evlist:
        ids = {e.get('awayTeamId'), e.get('homeTeamId')}
        if away_id in ids and home_id in ids:
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
        tmap = teams_for(mlg)
        away_id = tmap.get(norm_abbr(g.get('away')))
        home_id = tmap.get(norm_abbr(g.get('home')))
        evlist = events_for(mlg, date)
        print(f'DEBUG {mlg} {date}: teams={len(tmap)} sample={list(tmap.items())[:5]} '
              f'away={g.get("away")}->{away_id} home={g.get("home")}->{home_id} '
              f'events={len(evlist)} sample_ev={[{"a":e.get("awayTeamId"),"h":e.get("homeTeamId"),"an":e.get("awayTeamName"),"hn":e.get("homeTeamName")} for e in evlist[:3]]}',
              file=sys.stderr)
        ev = match_event(evlist, away_id, home_id)
        if not ev:
            # the game can fall on the next UTC calendar day for late local kickoffs
            try:
                nd = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
                ev = match_event(events_for(mlg, nd), away_id, home_id)
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
