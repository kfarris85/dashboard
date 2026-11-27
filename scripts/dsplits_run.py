"""Daily defensive-splits build for the dashboard's props tab (GitHub Actions).

Replaces the Claude scheduled task "Defensive Splits Publish": same scripts, same
data sources, same sanity checks, same "never commit a timestamp-only change" rule.
Each league is independent: one failing keeps its existing file and the others still
publish. Any failure makes this script exit 1 so the workflow run shows red and
GitHub emails you.

usage: python3 -I scripts/dsplits_run.py <repo dir>
"""
import sys, os, json, subprocess, urllib.request, shutil, tempfile, time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
DATA, SCRIPTS = os.path.join(REPO, 'data'), os.path.join(REPO, 'scripts')
NOW = datetime.now(ZoneInfo('America/Chicago'))
TODAY, M, WD = NOW.date(), NOW.month, NOW.strftime('%A')
FORCE = os.environ.get('FORCE_ALL') == '1'   # manual run: ignore the day-of-week rules
TMP = tempfile.mkdtemp(prefix='dsplits_')
problems, done = [], []


def log(*a):
    print(*a, flush=True)


def fetch(url, path=None, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (dashboard dsplits)'})
            with urllib.request.urlopen(req, timeout=120) as r:
                b = r.read()
            if path:
                open(path, 'wb').write(b)
                return path
            return json.loads(b)
        except Exception as e:
            if getattr(e, 'code', None) == 404 or i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def run(*args):
    r = subprocess.run([sys.executable, '-I', *args], capture_output=True, text=True)
    log(r.stdout[-1500:])
    if r.returncode:
        raise RuntimeError(f'{os.path.basename(args[0])} failed: {r.stderr[-600:]}')


def espn_week(path):
    d = fetch(f'https://site.api.espn.com/apis/site/v2/sports/football/{path}/scoreboard')
    return (d.get('week') or {}).get('number'), (d.get('season') or {}).get('type')


def validate(out):
    d = json.load(open(out))
    for k in ('teams', 'cats', 'n'):
        if k not in d:
            raise RuntimeError(f'missing "{k}"')
    if d['n'] != len(d['teams']):
        raise RuntimeError(f'n={d["n"]} but {len(d["teams"])} team entries')
    for t, v in d['teams'].items():
        miss = [c for c in d['cats'] if not isinstance(v.get(c), dict) or v[c].get('rank') is None]
        if miss:
            raise RuntimeError(f'team {t} has no rank for {miss[:3]}')
    return d


def publish(name, out, check):
    """Validate the new file, apply the league's sanity check, and only replace the repo's
    copy when the team numbers actually changed."""
    dest = os.path.join(DATA, name)
    d = validate(out)
    check(d)
    if os.path.exists(dest):
        try:
            if json.load(open(dest)).get('teams') == d['teams']:
                log(f'{name}: unchanged, keeping repo copy')
                return
        except Exception:
            pass
    shutil.copy(out, dest)
    done.append(name)
    log(f'{name}: updated ({d["n"]} teams)')


def league(name, fn):
    try:
        fn()
    except Exception as e:
        problems.append(f'{name}: {e}')
        log(f'!! {name} failed: {e}')


def exists(name):
    return os.path.exists(os.path.join(DATA, name))


# ---------------- NFL ----------------
def nfl():
    if not (M >= 9 or M <= 2):
        return log('NFL: offseason, skip')
    if not FORCE and WD not in ('Monday', 'Tuesday', 'Friday') and exists('nfl_dsplits.json'):
        return log('NFL: not a run day, skip')
    season = TODAY.year if M >= 3 else TODAY.year - 1
    d = os.path.join(TMP, 'nfl'); os.makedirs(d)
    fetch(f'https://github.com/nflverse/nflverse-data/releases/download/pbp/play_by_play_{season}.parquet',
          os.path.join(d, f'play_by_play_{season}.parquet'))
    out = os.path.join(TMP, 'nfl_dsplits.json')
    run(os.path.join(SCRIPTS, 'nfl_dsplits.py'), d, out, str(season))
    wk, _ = espn_week('nfl')

    def chk(j):
        if j['n'] != 32:
            raise RuntimeError(f'expected 32 teams, got {j["n"]}')
        if wk and abs(int(j.get('through_week') or 0) - wk) > 1:
            raise RuntimeError(f'through_week {j.get("through_week")} vs current NFL week {wk}')
    publish('nfl_dsplits.json', out, chk)


# ---------------- men's college basketball ----------------
def cbb():
    if not (M >= 11 or M <= 3 or (M == 4 and TODAY.day <= 10)):
        return log('CBB: offseason, skip')
    season = TODAY.year + 1 if M >= 8 else TODAY.year
    p = fetch(f'https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-data/main/mbb/team_box/parquet/team_box_{season}.parquet',
              os.path.join(TMP, f'cbb_team_box_{season}.parquet'))
    out = os.path.join(TMP, 'cbb_dsplits.json')
    run(os.path.join(SCRIPTS, 'hoops_dsplits.py'), p, out, 'cbb', '2')

    def chk(j):
        if j['n'] < 300:
            raise RuntimeError(f'only {j["n"]} teams (need 300+)')
        th = date.fromisoformat(j['through'])
        if (TODAY - th).days > 2:
            raise RuntimeError(f'data only through {th} (hoopR feed may be stale)')
    publish('cbb_dsplits.json', out, chk)


# ---------------- NBA ----------------
def nba():
    if not (M >= 10 or M <= 6) or (M == 10 and TODAY.day < 18):
        return log('NBA: offseason, skip')
    season = TODAY.year + 1 if M >= 8 else TODAY.year
    p = fetch(f'https://raw.githubusercontent.com/sportsdataverse/hoopr-nba-data/main/nba/team_box/parquet/team_box_{season}.parquet',
              os.path.join(TMP, f'nba_team_box_{season}.parquet'))
    out = os.path.join(TMP, 'nba_dsplits.json')
    run(os.path.join(SCRIPTS, 'hoops_dsplits.py'), p, out, 'nba', '3')

    def chk(j):
        if j['n'] != 30:
            raise RuntimeError(f'expected 30 teams, got {j["n"]} (an exhibition side may have slipped in) — not written')
    publish('nba_dsplits.json', out, chk)


# ---------------- college football ----------------
def cfb():
    if not (M >= 9 or M == 1 or (M == 8 and TODAY.day >= 20)):
        return log('CFB: offseason, skip')
    wk, _ = espn_week('college-football')
    cur = None
    if exists('cfb_dsplits.json'):
        try:
            cur = json.load(open(os.path.join(DATA, 'cfb_dsplits.json'))).get('through_week')
        except Exception:
            cur = None
    behind = cur is None or (wk and wk - int(cur) >= 2)
    if not FORCE and WD not in ('Sunday', 'Monday') and not behind:
        return log('CFB: not a run day, skip')
    ranks = os.path.join(DATA, 'cfb_ranks.json')
    if not os.path.exists(ranks):
        raise RuntimeError('cfb_ranks.json missing, so teams cannot be keyed by ESPN id — skipped')
    season = TODAY.year if M >= 3 else TODAY.year - 1
    p = fetch(f'https://raw.githubusercontent.com/sportsdataverse/cfbfastR-data/main/player_stats/parquet/player_stats_{season}.parquet',
              os.path.join(TMP, f'player_stats_{season}.parquet'))
    out = os.path.join(TMP, 'cfb_dsplits.json')
    run(os.path.join(SCRIPTS, 'cfb_dsplits.py'), p, out, ranks, '3')

    def chk(j):
        if j['n'] < 120:
            raise RuntimeError(f'only {j["n"]} teams (need 120+)')
        if j.get('keyed') != 'espn_id':
            raise RuntimeError('not keyed by ESPN id')
        if wk and abs(int(j.get('through_week') or 0) - wk) > 1:
            raise RuntimeError(f'through_week {j.get("through_week")} vs current CFB week {wk}')
    publish('cfb_dsplits.json', out, chk)


# ---------------- MLB ----------------
def mlb():
    if not (3 <= M <= 10) and exists('mlb_dsplits.json'):
        return log('MLB: offseason, skip')
    season = TODAY.year if M >= 3 else TODAY.year - 1
    d = os.path.join(TMP, 'mlb'); os.makedirs(d)
    for tid in range(1, 31):
        try:
            j = fetch(f'https://sports.core.api.espn.com/v2/sports/baseball/leagues/mlb/seasons/{season}/types/2/teams/{tid}/statistics')
            json.dump(j, open(os.path.join(d, f'{tid}.json'), 'w'))
        except Exception as e:
            log(f'MLB team {tid}: {e}')
    out = os.path.join(TMP, 'mlb_dsplits.json')
    run(os.path.join(SCRIPTS, 'mlb_dsplits.py'), d, out, str(season), '20')

    def chk(j):
        if j['n'] < 28:
            raise RuntimeError(f'only {j["n"]} teams')
        avg = lambda c: sum(t[c]['v'] for t in j['teams'].values()) / len(j['teams'])
        for c in ('hits_allowed', 'k_per9'):
            if c in j['cats'] and not 7 <= avg(c) <= 10.5:
                raise RuntimeError(f'league {c} averages {avg(c):.2f} per 9 — not baseball-sane')
    publish('mlb_dsplits.json', out, chk)


for n, f in (('NFL', nfl), ('CBB', cbb), ('NBA', nba), ('CFB', cfb), ('MLB', mlb)):
    league(n, f)

log('\nUPDATED: ' + (', '.join(done) or 'nothing'))
if problems:
    log('PROBLEMS:\n  ' + '\n  '.join(problems))
    sys.exit(1)
