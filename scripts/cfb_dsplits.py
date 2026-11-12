"""Per-category defensive splits for the college football weekly model.

Reads cfbfastR's player_stats parquet (one row per player-play, with the player's own
team and the opponent) and works out what each defense ALLOWS in the categories player
props are written on, adjusted for the offenses that defense actually played.

Row semantics in that file: `team` is the team the credited PLAYER belongs to, so for
offensive stats (receptions, completions, rushes) `team` is the offense and `opponent`
is the defense, while for defensive stats (sacks, interceptions) `team` IS the defense.

usage: python3 -I cfb_dsplits.py <player_stats.parquet> <out.json> [cfb_ranks.json] [min games]
  cfb_ranks.json (the model's published ratings) maps team names to ESPN team ids, which
  is how the dashboard looks teams up. Without it the file is keyed by team name.
"""
import sys, json
import numpy as np, pandas as pd
from datetime import datetime, timezone

SRC, OUT = sys.argv[1], sys.argv[2]
RANKS = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] != '-' else None
MING = int(sys.argv[4]) if len(sys.argv) > 4 else 3

CATS = {
    'pass_yds': (True,  'pass yards allowed per game'),
    'rush_yds': (True,  'rush yards allowed per game'),
    'rec':      (True,  'catches allowed per game'),
    'cmp':      (True,  'completions allowed per game'),
    'pass_td':  (True,  'passing TDs allowed per game'),
    'rush_td':  (True,  'rushing TDs allowed per game'),
    'plays':    (True,  'plays faced per game'),
}


def nz(s):
    """cfbfastR writes blanks, not nulls, in the player columns."""
    return s.replace('', np.nan)


def main():
    d = pd.read_parquet(SRC)
    for c in ('reception_player', 'completion_player', 'rush_player', 'sack_player',
              'interception_player', 'touchdown_player', 'fumble_recovered_player'):
        if c in d:
            d[c] = nz(d[c].astype('string'))
    for c in ('reception_yds', 'completion_yds', 'rush_yds'):
        d[c] = pd.to_numeric(d[c], errors='coerce')

    # which plays were a pass and which a run, so touchdowns can be split
    play = ['game_id', 'play_id']
    is_pass = d.groupby(play).completion_player.apply(lambda s: s.notna().any())
    is_rush = d.groupby(play).rush_player.apply(lambda s: s.notna().any())
    td = d[d.touchdown_player.notna()].set_index(play)
    td['pass_td'] = is_pass.reindex(td.index).fillna(False).astype(int)
    td['rush_td'] = is_rush.reindex(td.index).fillna(False).astype(int)
    td = td.reset_index()

    off = d[d.completion_player.notna() | d.rush_player.notna() | d.reception_player.notna()]
    games_as_d = d.groupby('opponent').game_id.nunique().rename('g')      # games that defense played
    games_as_o = d.groupby('team').game_id.nunique().rename('g')

    def per_game(frame, by, value=None, count=False):
        g = frame.groupby(by)
        s = g.size() if count else g[value].sum()
        return s

    allowed = pd.DataFrame(index=games_as_d.index)
    allowed['pass_yds'] = per_game(d[d.completion_player.notna()], 'opponent', 'completion_yds')
    allowed['rush_yds'] = per_game(d[d.rush_player.notna()], 'opponent', 'rush_yds')
    allowed['rec'] = per_game(d[d.reception_player.notna()], 'opponent', count=True)
    allowed['cmp'] = per_game(d[d.completion_player.notna()], 'opponent', count=True)
    allowed['pass_td'] = per_game(td[td.pass_td == 1], 'opponent', count=True)
    allowed['rush_td'] = per_game(td[td.rush_td == 1], 'opponent', count=True)
    allowed['plays'] = per_game(off, 'opponent', count=True)
    # sacks and interceptions are only patchily credited in this feed, so they are left out
    allowed = allowed.fillna(0).div(games_as_d, axis=0)

    # the same measures from the offense's side, to judge who each defense faced
    offp = pd.DataFrame(index=games_as_o.index)
    offp['pass_yds'] = per_game(d[d.completion_player.notna()], 'team', 'completion_yds')
    offp['rush_yds'] = per_game(d[d.rush_player.notna()], 'team', 'rush_yds')
    offp['rec'] = per_game(d[d.reception_player.notna()], 'team', count=True)
    offp['cmp'] = per_game(d[d.completion_player.notna()], 'team', count=True)
    offp['pass_td'] = per_game(td[td.pass_td == 1], 'team', count=True)
    offp['rush_td'] = per_game(td[td.rush_td == 1], 'team', count=True)
    offp['plays'] = per_game(off, 'team', count=True)
    offp = offp.fillna(0).div(games_as_o, axis=0)

    lg = {c: float(offp[c].mean()) for c in CATS}
    pairs = d[['opponent', 'team', 'game_id']].drop_duplicates()
    faced = pairs.merge(offp.rename(columns={c: c + '_o' for c in CATS}), left_on='team', right_index=True, how='left')
    sos = faced.groupby('opponent')[[c + '_o' for c in CATS]].mean()

    ids = {}
    if RANKS:
        try:
            R = json.load(open(RANKS))
            for tid, e in R.get('teams', {}).items():
                if e.get('team'):
                    ids[e['team']] = tid
        except Exception:
            ids = {}
    keep = sorted(set(games_as_d.index[games_as_d >= MING]) & set(allowed.index) & set(sos.index))
    # rank FBS against FBS: an FCS side only appears in the file for the one game it
    # played up, and its numbers are not comparable
    if ids:
        keep = [t for t in keep if t in ids]

    adj, teams = {}, {}
    for c, (lower_better, label) in CATS.items():
        a = (allowed[c] - (sos[c + '_o'] - lg[c])).reindex(keep)
        adj[c] = a
        r = a.rank(ascending=lower_better, method='min').astype(int)
        for t in keep:
            key = ids.get(t, t)
            teams.setdefault(key, {})[c] = {'v': round(float(allowed[c][t]), 2),
                                            'adj': round(float(a[t]), 2), 'rank': int(r[t])}
    for t in keep:
        key = ids.get(t, t)
        teams[key]['g'] = int(games_as_d[t])
        teams[key]['abbr'] = (R.get('teams', {}).get(ids[t], {}).get('abbr') or t) if ids and t in ids else t
        teams[key]['team'] = t

    out = {'updated': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
           'league': 'cfb', 'season': int(d.season.max()), 'through_week': int(d.week.max()),
           'source': f'cfbfastR player stats, schedule-adjusted (cfb_dsplits.py), min {MING} games',
           'keyed': 'espn_id' if ids else 'team_name', 'n': len(teams),
           'cats': {c: {'label': CATS[c][1], 'lowerBetter': CATS[c][0], 'lg': round(lg[c], 2)} for c in CATS},
           'teams': teams}
    json.dump(out, open(OUT, 'w'), ensure_ascii=False)
    show = pd.DataFrame({c: adj[c] for c in CATS}).round(1)
    print(f"{len(teams)} teams through week {out['through_week']} ({'ESPN ids' if ids else 'names'})")
    print(show.sort_values('pass_yds').head(5).to_string())
    print('...')
    print(show.sort_values('pass_yds').tail(3).to_string())
    pr, rr = adj['pass_yds'].rank(method='min'), adj['rush_yds'].rank(method='min')
    print(f"rank gap pass vs rush: median {int((pr-rr).abs().median())} places, max {int((pr-rr).abs().max())}")


if __name__ == '__main__':
    main()
