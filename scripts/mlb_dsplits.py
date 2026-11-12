"""Per-category opponent ratings for the MLB daily model.

In baseball the thing a prop runs into depends on who is hitting the ball. For a
HITTER, the opponent is the other team's pitching staff; for a PITCHER, it is the
other team's lineup. This writes both sides so the dashboard can pick the right one
from the player's position.

Every category is expressed as "the opponent's contribution toward this stat
happening", so lower always means harder for the player to go over, and rank 1 is
always the toughest matchup.

Input: a folder of ESPN core-API team statistics JSONs, one per club, each the body of
  https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/seasons/<YEAR>/types/2/teams/<ESPN team id>/statistics
named <espn team id>.json. The scheduled task fetches these (the shell cannot reach
ESPN); this script only does the arithmetic.

usage: python3 -I mlb_dsplits.py <json dir> <out.json> <season> [min games]

NOTE: this endpoint gives season totals, not game logs, so unlike the football and
basketball splits these rates are NOT strength-of-schedule adjusted. Over a 162-game
MLB season schedules are close enough to balanced that the distortion is small, but
it is real in April and in interleague-heavy stretches. The output says so.
"""
import sys, os, json, glob
from datetime import datetime, timezone

SRC, OUT, SEASON = sys.argv[1], sys.argv[2], int(sys.argv[3])
MING = int(sys.argv[4]) if len(sys.argv) > 4 else 20

# name -> (category it comes from, stat name, per-9 or per-game, label, who it is for)
CATS = {
    'hits_allowed': ('pitching', 'hits',      'nine', 'hits allowed per 9 innings',       'batter'),
    'hr_allowed':   ('pitching', 'homeRuns',  'nine', 'home runs allowed per 9 innings',  'batter'),
    'runs_allowed': ('pitching', 'runs',      'nine', 'runs allowed per 9 innings',       'batter'),
    'bb_allowed':   ('pitching', 'walks',     'nine', 'walks allowed per 9 innings',      'batter'),
    'k_per9':       ('pitching', 'strikeouts', 'nine', 'strikeouts per 9 innings',        'batter'),
    'off_hits':     ('batting',  'hits',      'game', 'hits per game',                    'pitcher'),
    'off_runs':     ('batting',  'runs',      'game', 'runs per game',                    'pitcher'),
    'off_hr':       ('batting',  'homeRuns',  'game', 'home runs per game',               'pitcher'),
    'off_bb':       ('batting',  'walks',     'game', 'walks per game',                   'pitcher'),
    'off_k':        ('batting',  'strikeouts', 'game', 'strikeouts per game',             'pitcher'),
}


def stat(splits, cat, name):
    for c in splits.get('categories', []):
        if c.get('name') == cat:
            for s in c.get('stats', []):
                if s.get('name') == name:
                    try:
                        return float(s.get('value'))
                    except (TypeError, ValueError):
                        return None
    return None


def main():
    rows = {}
    for f in sorted(glob.glob(os.path.join(SRC, '*.json'))):
        tid = os.path.splitext(os.path.basename(f))[0]
        try:
            d = json.load(open(f))
        except Exception:
            continue
        sp = d.get('splits') or {}
        ip = stat(sp, 'pitching', 'innings')
        gp = stat(sp, 'batting', 'teamGamesPlayed') or stat(sp, 'pitching', 'teamGamesPlayed')
        if not ip or not gp or gp < MING:
            continue
        # ESPN writes innings as 1436.1 meaning 1436 and 1/3
        whole = int(ip)
        ip9 = whole + round((ip - whole) * 10) / 3.0
        r = {'g': int(gp), 'ip': round(ip9, 1)}
        for key, (cat, name, how, label, _who) in CATS.items():
            v = stat(sp, cat, name)
            if v is None:
                continue
            r[key] = (v * 9.0 / ip9) if how == 'nine' else (v / gp)
        rows[tid] = r

    if len(rows) < 20:
        print(f'only {len(rows)} teams parsed — not writing', file=sys.stderr)
        sys.exit(1)

    teams, lg = {}, {}
    for key, (cat, name, how, label, who) in CATS.items():
        vals = {t: r[key] for t, r in rows.items() if key in r}
        if not vals:
            continue
        lg[key] = sum(vals.values()) / len(vals)
        order = sorted(vals, key=lambda t: vals[t])   # lower = tougher for the player
        for i, t in enumerate(order):
            teams.setdefault(t, {})[key] = {'v': round(vals[t], 2), 'adj': round(vals[t], 2), 'rank': i + 1}
    for t, r in rows.items():
        if t in teams:
            teams[t]['g'] = r['g']

    out = {'updated': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
           'league': 'mlb', 'season': SEASON,
           'source': 'ESPN team season statistics (mlb_dsplits.py) — season rates, not schedule-adjusted',
           'adjusted': False, 'n': len(teams),
           'cats': {k: {'label': v[3], 'lowerBetter': True, 'for': v[4], 'lg': round(lg.get(k, 0), 2)}
                    for k, v in CATS.items() if k in lg},
           'teams': teams}
    json.dump(out, open(OUT, 'w'), ensure_ascii=False)
    print(f'{len(teams)} teams, {SEASON}')
    for k in ('hits_allowed', 'k_per9', 'off_k'):
        if k not in lg:
            continue
        best = sorted(teams, key=lambda t: teams[t][k]['rank'])[:3]
        print(f"  {k:13s} league {lg[k]:6.2f} | toughest: " +
              ', '.join(f"{t}={teams[t][k]['v']}" for t in best))


if __name__ == '__main__':
    main()
