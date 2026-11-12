"""Per-category defensive splits for the NFL weekly model.

Turns nflfastR play-by-play into what each defense ALLOWS in the categories player
props are actually written on, strength-of-schedule adjusted the same way teams.py
adjusts EPA: subtract how good the offenses that defense happened to face have been.

usage: python3 -I nfl_dsplits.py <pbp dir> <out.json> [seasons csv] [last N team-games]
  <pbp dir> holds nflfastR play_by_play_<season>.parquet
Writes <out.json>: {"updated","through","season","n","cats":{...},"teams":{ABBR:{cat:{v,adj,rank}}}}
"""
import sys, json
import numpy as np, pandas as pd
from datetime import datetime, timezone

D = sys.argv[1]
OUT = sys.argv[2]
SEASONS = [int(x) for x in sys.argv[3].split(',')] if len(sys.argv) > 3 else [2026]
LASTN = int(sys.argv[4]) if len(sys.argv) > 4 else 25

COLS = ['season', 'week', 'game_id', 'season_type', 'posteam', 'defteam', 'play_type',
        'yards_gained', 'epa', 'pass_touchdown', 'rush_touchdown', 'sack', 'complete_pass',
        'interception', 'fumble_lost', 'two_point_attempt', 'rush_attempt', 'pass_attempt']

# name -> (per-game or per-play, lower value = better defense)
CATS = {
    'pass_yds':  ('game', True,  'pass yards allowed per game'),
    'rush_yds':  ('game', True,  'rush yards allowed per game'),
    'cmp':       ('game', True,  'completions allowed per game'),
    'pass_td':   ('game', True,  'passing TDs allowed per game'),
    'rush_td':   ('game', True,  'rushing TDs allowed per game'),
    'sack':      ('game', False, 'sacks per game'),
    'takeaway':  ('game', False, 'takeaways per game'),
    'pass_epa':  ('play', True,  'EPA allowed per dropback'),
    'rush_epa':  ('play', True,  'EPA allowed per rush'),
    'plays':     ('game', True,  'scrimmage plays faced per game'),
}


def team_game(pbp):
    """One row per (game, offense): what that offense produced."""
    nt = pbp.two_point_attempt.fillna(0) == 0
    scrim = pbp[pbp.play_type.isin(['pass', 'run', 'qb_kneel', 'qb_spike']) & nt]
    p = pbp[(pbp.play_type == 'pass') & nt]
    r = pbp[(pbp.play_type == 'run') & nt]
    k = ['game_id', 'posteam', 'defteam']
    g = scrim.groupby(k).agg(plays=('play_type', 'size')).reset_index()

    def add(df, name, how):
        s = df.groupby(k).agg(**{name: how}).reset_index()
        return s
    g = g.merge(add(p, 'pass_yds', ('yards_gained', 'sum')), on=k, how='left')
    g = g.merge(add(r, 'rush_yds', ('yards_gained', 'sum')), on=k, how='left')
    g = g.merge(add(p, 'cmp', ('complete_pass', 'sum')), on=k, how='left')
    g = g.merge(add(p, 'pass_td', ('pass_touchdown', 'sum')), on=k, how='left')
    g = g.merge(add(r, 'rush_td', ('rush_touchdown', 'sum')), on=k, how='left')
    g = g.merge(add(p, 'sack', ('sack', 'sum')), on=k, how='left')
    to = pbp[nt].groupby(k).agg(ints=('interception', 'sum'), fum=('fumble_lost', 'sum')).reset_index()
    to['takeaway'] = to.ints.fillna(0) + to.fum.fillna(0)
    g = g.merge(to[k + ['takeaway']], on=k, how='left')
    # per-play efficiency needs its own denominators
    pe = p[p.epa.notna()].groupby(k).agg(pass_epa_s=('epa', 'sum'), pass_n=('epa', 'size')).reset_index()
    re = r[r.epa.notna()].groupby(k).agg(rush_epa_s=('epa', 'sum'), rush_n=('epa', 'size')).reset_index()
    g = g.merge(pe, on=k, how='left').merge(re, on=k, how='left')
    return g.fillna(0)


def rates(df, side):
    """Per-team rate in every category. side='posteam' -> offense produced,
    side='defteam' -> what that team allowed."""
    out = {}
    grp = df.groupby(side)
    n = grp.game_id.nunique()
    for c, (kind, _, _) in CATS.items():
        if kind == 'game':
            out[c] = grp[c].sum() / n
        elif c == 'pass_epa':
            out[c] = grp.pass_epa_s.sum() / grp.pass_n.sum()
        elif c == 'rush_epa':
            out[c] = grp.rush_epa_s.sum() / grp.rush_n.sum()
    o = pd.DataFrame(out)
    o['g'] = n
    return o


def main():
    pbp = pd.concat([pd.read_parquet(f'{D}/play_by_play_{y}.parquet', columns=COLS) for y in SEASONS])
    pbp = pbp[(pbp.season_type == 'REG') & pbp.posteam.notna() & pbp.defteam.notna()]
    tg = team_game(pbp)
    # keep each defense's most recent LASTN games, same window the ratings use
    order = pbp.groupby('game_id').week.max().rename('week')
    tg = tg.merge(order, on='game_id', how='left').sort_values('week')
    tg = tg.groupby('defteam').tail(LASTN * 1)  # one row per opponent game already

    off = rates(tg, 'posteam')     # what each offense produces
    dfn = rates(tg, 'defteam')     # what each defense allows
    lg = {c: float(off[c].mean()) for c in CATS}

    # strength of schedule: average offense faced, in each category
    offr = off[list(CATS)].rename(columns={c: c + '_off' for c in CATS}).reset_index()
    offr = offr.rename(columns={offr.columns[0]: 'posteam'})
    faced = tg[['defteam', 'posteam']].merge(offr, on='posteam', how='left')
    sos = faced.groupby('defteam')[[c + '_off' for c in CATS]].mean()

    teams = {}
    adj = {}
    for c, (kind, lower_better, label) in CATS.items():
        a = dfn[c] - (sos[c + '_off'] - lg[c])   # + = faced better offenses, so credit the defense
        adj[c] = a
    for c, (kind, lower_better, label) in CATS.items():
        r = adj[c].rank(ascending=lower_better, method='min').astype(int)
        for t in dfn.index:
            teams.setdefault(t, {})[c] = {'v': round(float(dfn[c][t]), 3),
                                          'adj': round(float(adj[c][t]), 3),
                                          'rank': int(r[t])}
    for t in dfn.index:
        teams[t]['g'] = int(dfn['g'][t])
    out = {'updated': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
           'season': max(SEASONS), 'through_week': int(pbp.week.max()),
           'source': 'nflfastR play-by-play, schedule-adjusted (nfl_dsplits.py)',
           'n': len(teams),
           'cats': {c: {'label': CATS[c][2], 'lowerBetter': CATS[c][1], 'lg': round(lg[c], 3)} for c in CATS},
           'teams': teams}
    json.dump(out, open(OUT, 'w'), indent=1)
    show = pd.DataFrame({c: adj[c] for c in CATS}).round(2)
    show['passRk'] = adj['pass_yds'].rank(method='min').astype(int)
    show['rushRk'] = adj['rush_yds'].rank(method='min').astype(int)
    print(f"{len(teams)} teams through week {int(pbp.week.max())}")
    print(show.sort_values('pass_yds').head(6).to_string())
    print('...')
    print(show.sort_values('pass_yds').tail(4).to_string())


if __name__ == '__main__':
    main()
