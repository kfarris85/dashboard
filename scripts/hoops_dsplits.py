"""Per-category defensive splits for the basketball models (NBA and men's college).

Reads a hoopR team-box parquet (one row per team-game, with the opponent on the row)
and works out what each defense ALLOWS in the categories player props are written on,
adjusted for the quality of the offenses that defense happened to face and for pace.

usage: python3 -I hoops_dsplits.py <team_box.parquet> <out.json> <league: nba|cbb> [min games]

Writes {"updated","league","n","cats":{...},"teams":{TEAM_ID:{cat:{v,adj,rank}}}}
keyed by ESPN team id, the same key the dashboard uses.
"""
import sys, json
import numpy as np, pandas as pd
from datetime import datetime, timezone

BOX, OUT, LEAGUE = sys.argv[1], sys.argv[2], sys.argv[3]
MING = int(sys.argv[4]) if len(sys.argv) > 4 else 3

# category -> (column on the OFFENSE's row, lower = better defense, label)
CATS = {
    'pts':   ('team_score', True,  'points allowed per game'),
    'reb':   ('total_rebounds', True,  'rebounds allowed per game'),
    'ast':   ('assists', True,  'assists allowed per game'),
    'tpm':   ('three_point_field_goals_made', True,  '3-pointers allowed per game'),
    'fgm':   ('field_goals_made', True,  'field goals allowed per game'),
    'ftm':   ('free_throws_made', True,  'free throws allowed per game'),
    'stl':   ('turnovers', False, 'turnovers forced per game'),
    'blk':   ('blocks', False, 'blocks per game'),
    'poss':  ('_poss', True,  'possessions per game (pace)'),
}


def main():
    b = pd.read_parquet(BOX)
    b = b[b.team_score.notna() & b.opponent_team_id.notna()].copy()
    if 'season_type' in b:
        b = b[b.season_type == 2]
    b['team_id'] = b.team_id.astype(str)
    b['opponent_team_id'] = b.opponent_team_id.astype(str)
    # possessions, the standard estimate
    b['_poss'] = (b.field_goals_attempted.fillna(0) - b.offensive_rebounds.fillna(0)
                  + b.turnovers.fillna(0) + 0.44 * b.free_throws_attempted.fillna(0))

    cols = [c for c, _, _ in CATS.values()]
    off = b.groupby('team_id')[cols].mean()          # what each offense produces
    g = b.groupby('team_id').size().rename('g')
    # what each defense allows = the stats their opponents put up against them
    allowed = b.groupby('opponent_team_id')[cols].mean()
    allowed.index.name = 'team_id'
    ag = b.groupby('opponent_team_id').size().rename('g')

    # All-Star and exhibition sides (STARS, WORLD, a G-League guest) show up with a
    # handful of games and would otherwise be ranked alongside real teams.
    med = float(ag.median()) if len(ag) else 0.0
    floor = max(MING, 0.4 * med)
    keep = sorted(set(allowed.index[ag >= floor]) & set(off.index))
    lg = {c: float(off[c].mean()) for c in cols}

    # strength of schedule: the average offense each defense faced
    faced = b[['opponent_team_id', 'team_id']].rename(columns={'opponent_team_id': 'd', 'team_id': 'o'})
    faced = faced.merge(off.rename(columns={c: c + '_o' for c in cols}), left_on='o', right_index=True, how='left')
    sos = faced.groupby('d')[[c + '_o' for c in cols]].mean()

    adj, teams = {}, {}
    for name, (col, lower_better, label) in CATS.items():
        a = allowed[col] - (sos[col + '_o'] - lg[col])
        a = a.reindex(keep)
        adj[name] = a
        r = a.rank(ascending=lower_better, method='min').astype(int)
        for t in keep:
            teams.setdefault(t, {})[name] = {'v': round(float(allowed[col][t]), 2),
                                             'adj': round(float(a[t]), 2), 'rank': int(r[t])}
    names = b.drop_duplicates('team_id').set_index('team_id')
    for t in keep:
        teams[t]['g'] = int(ag[t])
        if t in names.index:
            teams[t]['abbr'] = str(names.team_abbreviation.get(t, '') or '')

    out = {'updated': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
           'league': LEAGUE, 'through': str(b.game_date.max())[:10],
           'source': f'hoopR team box, schedule-adjusted (hoops_dsplits.py), min {MING} games',
           'minGames': round(floor, 1),
           'n': len(keep),
           'cats': {k: {'label': v[2], 'lowerBetter': v[1], 'lg': round(lg[v[0]], 2)} for k, v in CATS.items()},
           'teams': teams}
    json.dump(out, open(OUT, 'w'), ensure_ascii=False)
    show = pd.DataFrame({k: adj[k] for k in CATS}).round(2)
    show['abbr'] = [teams[t].get('abbr', '') for t in show.index]
    print(f"{len(keep)} teams through {out['through']} ({LEAGUE})")
    print(show.sort_values('pts').head(6).to_string())
    print('...')
    print(show.sort_values('pts').tail(3).to_string())
    # how different is a per-stat rank from the overall points rank?
    pr = adj['pts'].rank(method='min')
    for k in ('reb', 'ast', 'tpm'):
        kr = adj[k].rank(method='min')
        print(f"rank gap pts vs {k}: median {int((pr-kr).abs().median())} places, max {int((pr-kr).abs().max())}")


if __name__ == '__main__':
    main()
