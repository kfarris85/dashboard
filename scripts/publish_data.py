"""Dashboard data publish (GitHub Actions) — replaces the Claude task "Dashboard Data Publish".

Reads Kyle's model output from Google Drive / Sheets with the dashboard-bot service
account and writes the dashboard's data files:
  data/history.json   Season Tracker Log (once a day, after the 4:41 PM sync)
  data/picks.json     current picks for nfl / cfb / nba / cbb / mlb
  data/nfl_ranks.json NFL model rankings (re-run with the model's own scripts)
  data/nba_ranks.json NBA power file -> rankings
  data/cfb_ranks.json CFB model ratings (re-run with the model's own scripts)
  data/cbb_ranks.json college hoops ratings (via the model's dashboard_json.py)

Same rules as the Claude task: never edit Drive; use model scripts as-is; skip steps
whose inputs haven't changed; never rewrite a file whose content only differs by
timestamp; each step independent; every sanity check the task did is coded below and
a failed check keeps the existing file and fails the run (GitHub emails).

usage: python3 -I scripts/publish_data.py <repo dir> [--shadow]
  --shadow  build everything and print how it compares to the repo's files; write nothing
env:   GOOGLE_SA_JSON, FORCE_ALL=1 (ignore once-a-day / day-of-week rules)
NOTE:  this repo is public, so its Actions logs are too. Log counts and comparisons,
       never Drive file contents beyond what the dashboard already publishes.
"""
import sys, os, re, io, csv, json, base64, shutil, hashlib, subprocess, tempfile, time, urllib.request
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
SHADOW = '--shadow' in sys.argv
FORCE = os.environ.get('FORCE_ALL') == '1'
DATA = os.path.join(REPO, 'data')
CT = ZoneInfo('America/Chicago')
NOW = datetime.now(CT)
TODAY, YDAY = NOW.date(), NOW.date() - timedelta(days=1)
TMP = tempfile.mkdtemp(prefix='publish_')
MON = {m: i for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'], 1)}

info = json.loads(os.environ['GOOGLE_SA_JSON'])
S = AuthorizedSession(service_account.Credentials.from_service_account_info(
    info, scopes=['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/spreadsheets.readonly']))
DRIVE = 'https://www.googleapis.com/drive/v3/files'
problems, written, notes = [], [], []


def log(*a):
    print(*a, flush=True)


# ---------------- Google helpers ----------------
def ls(folder):
    out, tok = [], None
    while True:
        p = {'q': f"'{folder}' in parents and trashed=false", 'pageSize': 1000,
             'fields': 'nextPageToken,files(id,name,mimeType,createdTime,modifiedTime,size,webViewLink)'}
        if tok:
            p['pageToken'] = tok
        r = S.get(DRIVE, params=p); r.raise_for_status(); j = r.json()
        out += j.get('files', [])
        tok = j.get('nextPageToken')
        if not tok:
            return out


def newest(files, pred):
    c = [f for f in files if pred(f['name'])]
    return max(c, key=lambda f: f['createdTime']) if c else None


def download(f, path=None):
    r = S.get(f"{DRIVE}/{f['id']}", params={'alt': 'media'}); r.raise_for_status()
    if path:
        open(path, 'wb').write(r.content)
        if f.get('size') and int(f['size']) != len(r.content):
            raise RuntimeError(f"{f['name']}: downloaded {len(r.content)} bytes, Drive says {f['size']}")
        return path
    return r.content


def sheet_rows(fid, rng='A1:AZ'):
    r = S.get(f'https://sheets.googleapis.com/v4/spreadsheets/{fid}/values/{rng}'); r.raise_for_status()
    vals = r.json().get('values', [])
    if not vals:
        return []
    hdr = vals[0]
    return [dict(zip(hdr, row + [''] * (len(hdr) - len(row)))) for row in vals[1:] if any(c.strip() for c in row)]


def fetch(url, path, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (dashboard publish)'})
            with urllib.request.urlopen(req, timeout=300) as r:
                open(path, 'wb').write(r.read())
            return path
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def run(*args, env=None, cwd=None):
    r = subprocess.run(args, capture_output=True, text=True, env={**os.environ, **(env or {})}, cwd=cwd)
    log((r.stdout or '')[-1200:])
    if r.returncode:
        raise RuntimeError(f'{os.path.basename(str(args[1] if args[0] in (sys.executable, "bash") else args[0]))} failed: {(r.stderr or "")[-600:]}')


# ---------------- value helpers ----------------
def num(v):
    v = str(v if v is not None else '').strip().replace('%', '').replace('+', '').replace(',', '')
    if v in ('', '—', '-', 'nan', 'None'):
        return None
    try:
        x = float(v)
        return int(x) if x == int(x) else x
    except ValueError:
        return None


def txt(v):
    v = str(v or '').strip()
    return None if v in ('', '—', '-') else v


def put(d, k, v):
    if v is not None and v != '':
        d[k] = v


def title_date(title, year_hint):
    m = re.search(r'\((\w{3}) (\d{1,2})', title)
    if not m or m.group(1) not in MON:
        return None
    y = year_hint
    d = date(y, MON[m.group(1)], int(m.group(2)))
    if (d - TODAY).days > 200:
        d = d.replace(year=y - 1)
    elif (TODAY - d).days > 200:
        d = d.replace(year=y + 1)
    return d


def read_json(name):
    try:
        return json.load(open(os.path.join(DATA, name)))
    except Exception:
        return None


def step(name, fn):
    try:
        fn()
    except Exception as e:
        problems.append(f'{name}: {e}')
        log(f'!! {name} failed: {e}')


def emit(name, obj, same_as_existing):
    """Write data/<name> unless the meaningful content equals the existing file's."""
    old = read_json(name)
    if old is not None and same_as_existing(old, obj):
        log(f'{name}: unchanged')
        return
    if SHADOW:
        written.append(name)
        log(f'{name}: WOULD UPDATE (shadow)')
        if old is not None and isinstance(obj.get('teams'), dict):
            ot, nt = old.get('teams', {}), obj['teams']
            dif = [k for k in set(ot) | set(nt) if ot.get(k) != nt.get(k)]
            log(f'   {len(dif)} of {len(nt)} teams differ; through {old.get("through", old.get("asof"))} -> {obj.get("through", obj.get("asof"))}')
            for k in dif[:3]:
                log(f'   {k}: {json.dumps(ot.get(k))[:160]} -> {json.dumps(nt.get(k))[:160]}')
        if old is not None and isinstance(obj.get('rows'), list):
            orr, nr = old.get('rows', []), obj['rows']
            ok = {r['key']: r for r in orr}; nk = {r['key']: r for r in nr}
            log(f'   rows {len(orr)} -> {len(nr)}; same keys {len(set(ok) & set(nk))}; changed {sum(1 for k in ok if k in nk and ok[k] != nk[k])}; order same {[r["key"] for r in orr] == [r["key"] for r in nr][:len(orr)]}')
            for k in [k for k in ok if k in nk and ok[k] != nk[k]][:3]:
                log(f'   {k}: ' + json.dumps({f: (ok[k].get(f), nk[k].get(f)) for f in ok[k] if ok[k].get(f) != nk[k].get(f)}, ensure_ascii=False)[:300])
        return
    json.dump(obj, open(os.path.join(DATA, name), 'w'), ensure_ascii=False, separators=(',', ':'))
    written.append(name)
    log(f'{name}: updated')


def stamp():
    return NOW.isoformat(timespec='seconds')


# ================= STEP 1: history =================
SEASON_TRACKER = '1EZUyTMkSO98ZEse6VXqXLCoAlNa8GXXiZ48s-j_aGao'


def history():
    old = read_json('history.json')
    up = None
    try:
        up = datetime.fromisoformat(old['updated']) if old else None
    except Exception:
        pass
    sync_time = NOW.replace(hour=16, minute=41, second=0, microsecond=0)
    due = (NOW >= NOW.replace(hour=16, minute=50) and (up is None or up < sync_time)) or up is None or (NOW - up) > timedelta(hours=26)
    if not (due or FORCE or SHADOW):
        return log('history: not due (Season Tracker syncs ~4:41 PM CT), skip')
    vals, start = [], 1
    while True:
        r = S.get(f'https://sheets.googleapis.com/v4/spreadsheets/{SEASON_TRACKER}/values/Log!A{start}:V{start + 1999}')
        r.raise_for_status()
        block = r.json().get('values', [])
        if not block:
            break
        vals += block
        if len(block) < 2000:
            break
        start += 2000
    # Fixed A–V column order (the Log's header row is not reliably row 1). Skip a header row if one is present.
    COLS = ['Key', 'League', 'Season', 'Date', 'Away', 'Home', 'Pick', 'Model %', 'Vegas %', 'Best Pick', 'Final', 'Result',
            'Spread Pick', 'ATS', 'Formula Total', 'Market Total', 'Actual Total', 'Total Lean', 'O/U Result', 'CLV', 'Beat Close', 'Source']
    ix = {h: i for i, h in enumerate(COLS)}
    if not any(row and row[0].strip() == 'Key' for row in vals):
        notes.append('Season Tracker Log has no header row (read by fixed column order)')
    g = lambda row, h: (row[ix[h]] if ix.get(h) is not None and ix[h] < len(row) else '')
    rows, kept = [], 0
    for row in vals:
        if not row or row[0].strip() == 'Key' or not g(row, 'Key').strip() or not g(row, 'Result').strip():
            continue
        kept += 1
        rows.append({'key': g(row, 'Key'), 'lg': g(row, 'League'), 'season': g(row, 'Season'), 'date': g(row, 'Date'),
                     'away': g(row, 'Away'), 'home': g(row, 'Home'), 'pick': g(row, 'Pick'), 'win': num(g(row, 'Model %')),
                     'vegas': num(g(row, 'Vegas %')), 'best': g(row, 'Best Pick').strip() == '★', 'final': g(row, 'Final'),
                     'result': g(row, 'Result').strip(), 'spreadPick': g(row, 'Spread Pick'), 'ats': g(row, 'ATS').strip(),
                     'fTotal': num(g(row, 'Formula Total')), 'mTotal': num(g(row, 'Market Total')), 'aTotal': num(g(row, 'Actual Total')),
                     'lean': g(row, 'Total Lean'), 'ou': g(row, 'O/U Result').strip(), 'clv': num(g(row, 'CLV')), 'beat': g(row, 'Beat Close').strip()})
    # checks the Claude task made
    if len(rows) < kept:
        raise RuntimeError('lost rows converting the Log')
    for r in rows:
        if not all(r.get(k) not in (None, '') for k in ('date', 'away', 'home', 'pick', 'result')):
            raise RuntimeError(f'row {r["key"]} missing date/away/home/pick/result')
    if old and len(rows) < len(old.get('rows', [])):
        raise RuntimeError(f'Log has {len(rows)} graded rows, fewer than the {len(old["rows"])} already published')
    emit('history.json', {'updated': stamp(), 'source': 'Season Tracker Log', 'rows': rows},
         lambda o, n: o.get('rows') == n['rows'])


# ================= STEP 2: picks =================
picks = {}


def nfl_date(kick, created):
    m = re.search(r'(\d{1,2})/(\d{1,2})', kick or '')
    if not m:
        return None
    cy = int(created[:4])
    d = date(cy, int(m.group(1)), int(m.group(2)))
    if (date.fromisoformat(created[:10]) - d).days > 180:
        d = d.replace(year=cy + 1)
    return d


def nfl_game(r, created):
    d = nfl_date(r.get('Kickoff'), created)
    conf = r.get('Confidence', '')
    g = {'date': d.isoformat() if d else None, 'away': r['Away'].replace(' *', '').strip(), 'home': r['Home'].replace(' *', '').strip(),
         'pick': r['Probable Winner'].strip(), 'win': num(r['Win %']), 'conf': conf.replace('★', '').strip(), 'best': '★' in conf,
         'vegas': num(r.get('Vegas Win % (pick)'))}
    put(g, 'line', txt(r.get('Spread')))
    put(g, 'spreadPick', txt(r.get('Spread Pick')))
    put(g, 'cover', num(r.get('Cover %')))
    g['agrees'] = 'DISAGREE' not in (r.get('Model vs Vegas') or '').upper()
    pa, ph = num(r.get('Proj Away')), num(r.get('Proj Home'))
    if pa is not None and ph is not None:
        g['proj'] = [pa, ph]
    put(g, 'why', txt(r.get('Why')))
    put(g, 'fTotal', num(r.get('Formula Total'))); put(g, 'total', num(r.get('Market Total'))); put(g, 'lean', txt(r.get('Total Lean')))
    return g


def keep_date(g):
    return g.get('date') and g['date'] >= YDAY.isoformat()


def pk_nfl():
    if not (NOW.month >= 9 or NOW.month <= 2):
        picks['nfl'] = {'sheet': '', 'url': '', 'games': []}; return
    files = [f for f in ls('1tMDXXQCmn09oB-xTy6r4bbZYFbQoN4DK') if f['mimeType'].endswith('spreadsheet')]
    pat = re.compile(r'^(\d{4}) Week (\d+) Picks( — Saturday update)? \(')
    cand = [(f, pat.match(f['name'])) for f in files]
    cand = [(f, m) for f, m in cand if m and 'preliminary' not in f['name'].lower() and 'Results' not in f['name']]
    if not cand:
        picks['nfl'] = {'sheet': '', 'url': '', 'games': []}; return
    wk = max((int(m.group(1)), int(m.group(2))) for _, m in cand)
    wed = [f for f, m in cand if (int(m.group(1)), int(m.group(2))) == wk and not m.group(3)]
    sat = [f for f, m in cand if (int(m.group(1)), int(m.group(2))) == wk and m.group(3)]
    base = max(wed or sat, key=lambda f: f['createdTime'])
    games = {(g['away'], g['home']): g for g in (nfl_game(r, base['createdTime']) for r in sheet_rows(base['id']))}
    if sat and wed:
        s = max(sat, key=lambda f: f['createdTime'])
        for g in (nfl_game(r, s['createdTime']) for r in sheet_rows(s['id'])):
            games[(g['away'], g['home'])] = g
    picks['nfl'] = {'sheet': base['name'], 'url': base['webViewLink'], 'games': [g for g in games.values() if keep_date(g)]}


def pk_nba():
    if not (NOW.month >= 10 or NOW.month <= 6):
        picks['nba'] = {'sheet': '', 'url': '', 'games': []}; return
    files = [f for f in ls('1Y8OVacj2Le5uI5PbAOWrpRFp-BiEF5-V') if f['mimeType'].endswith('spreadsheet') and re.search(r' NBA Picks \(', f['name'])]
    want = [f for f in files if title_date(f['name'], TODAY.year) in (TODAY, YDAY)]
    want.sort(key=lambda f: f['createdTime'])
    byid = {}
    for f in want:  # oldest first, newest sheet's row wins
        for r in sheet_rows(f['id']):
            gid = str(r.get('Game ID', '')).strip()
            if not gid:
                continue
            hs = num(r.get('Home Spread'))
            line = None
            if hs is not None:
                line = f"{r['Home']} {hs:g}" if hs <= 0 else f"{r['Away']} -{hs:g}"
                line = line.replace(' 0', ' PK') if hs == 0 else line
            conf = r.get('Confidence', '')
            g = {'id': gid, 'date': r.get('Date'), 'away': r['Away'], 'home': r['Home'], 'pick': r['Probable Winner'],
                 'win': num(r['Win %']), 'conf': conf.replace('★', '').strip(), 'best': '★' in (r.get('Best Pick', '') + conf),
                 'vegas': num(r.get('Vegas Win % (pick)'))}
            put(g, 'kalshi', num(r.get('Kalshi Win % (pick)')))
            put(g, 'line', line)
            put(g, 'spreadPick', txt(r.get('Spread Pick'))); put(g, 'cover', num(r.get('Cover %')))
            g['agrees'] = not str(r.get('Disagrees with Vegas', '')).strip().lower().startswith(('y', 'true', '✓', 'disagree'))
            pa, ph = num(r.get('Proj Away')), num(r.get('Proj Home'))
            if pa is not None and ph is not None:
                g['proj'] = [pa, ph]
            put(g, 'fTotal', num(r.get('Formula Total'))); put(g, 'total', num(r.get('Market Total'))); put(g, 'lean', txt(r.get('Total Lean')))
            put(g, 'why', txt(r.get('Why')))
            byid[gid] = g
    latest = want[-1] if want else (max(files, key=lambda f: f['createdTime']) if files else None)  # task kept the newest sheet's title even with no games
    picks['nba'] = {'sheet': latest['name'] if latest else '', 'url': latest['webViewLink'] if latest else '',
                    'games': [g for g in byid.values() if keep_date(g)]}


def pk_cfb():
    if not (NOW.month >= 8 or NOW.month == 1):
        picks['cfb'] = {'sheet': '', 'url': '', 'games': []}; return
    files = ls('1hXyrZWt0QIPmmdXjPdoHDe5MYlghgqt_')
    pat = re.compile(r'^(\d{4}) CFB Week (\d+) Picks \((\w{3} \d{1,2})\)$')
    sh = newest([f for f in files if f['mimeType'].endswith('spreadsheet')], lambda n: bool(pat.match(n)))
    if not sh:
        picks['cfb'] = {'sheet': '', 'url': '', 'games': []}; return
    dates = {}
    dc = newest(files, lambda n: n == sh['name'] + ' data.csv')
    if dc:
        for r in csv.DictReader(io.StringIO(download(dc).decode('utf-8-sig'))):
            dates[(r['away'], r['home'])] = r['game_date']
    sd = date.fromisoformat(sh['createdTime'][:10])
    wd = {'Mon': 0, 'Tue': 1, 'Wed': 2, 'Thu': 3, 'Fri': 4, 'Sat': 5, 'Sun': 6}
    out = []
    for r in sheet_rows(sh['id']):
        d = dates.get((r['Away'], r['Home']))
        if not d:  # fall back: kickoff weekday -> next such date after the sheet date
            m = re.search(r'(Mon|Tue|Wed|Thu|Fri|Sat|Sun)', r.get('Kickoff', ''))
            if m:
                dd = sd + timedelta(days=(wd[m.group(1)] - sd.weekday()) % 7 or 7)
                d = dd.isoformat()
        g = {'date': d, 'away': r['Away'], 'home': r['Home'], 'pick': r['Pick'], 'win': num(r['Model Win %']),
             'conf': (r.get('Confidence') or '').replace('★', '').strip(), 'best': '★' in (r.get('Best Pick') or ''),
             'vegas': num(r.get('Vegas Win % (pick)'))}
        put(g, 'line', txt(r.get('Line')))
        g['agrees'] = (r.get('Agrees w/ Vegas') or '').strip().lower() not in ('no', 'false')
        put(g, 'margin', num(r.get('Proj Margin')))
        put(g, 'fTotal', num(r.get('Formula Total'))); put(g, 'total', num(r.get('Market Total'))); put(g, 'lean', txt(r.get('Total Lean')))
        put(g, 'why', txt(r.get('Why')))
        out.append(g)
    picks['cfb'] = {'sheet': sh['name'], 'url': sh['webViewLink'], 'games': [g for g in out if keep_date(g)]}


def pk_mlb():
    if not (3 <= NOW.month <= 11):
        picks['mlb'] = {'sheet': '', 'url': '', 'games': []}; return
    files = [f for f in ls('1hcBhJsNAe0mm3QOCxSduk8gXFksoeefs') if f['mimeType'].endswith('spreadsheet')
             and re.search(r'(MLB|World Series) Picks \(', f['name'])]
    want = sorted([f for f in files if title_date(f['name'], TODAY.year) in (TODAY, YDAY)], key=lambda f: f['createdTime'])
    games = {}
    for f in want:
        for r in sheet_rows(f['id']):
            conf = r.get('Confidence', '')
            g = {'date': r.get('Date'), 'away': r['Away'], 'home': r['Home'], 'pick': r['Probable Winner'], 'win': num(r['Win %']),
                 'conf': conf.replace('★', '').strip(), 'best': '★' in (r.get('Best Pick', '') + conf), 'vegas': num(r.get('Vegas Win % (pick)')),
                 'agrees': True}
            put(g, 'fTotal', num(r.get('Formula Total'))); put(g, 'total', num(r.get('Market Total'))); put(g, 'lean', txt(r.get('Total Lean')))
            put(g, 'why', txt(r.get('Why')))
            games[(g['date'], g['away'], g['home'])] = g
    picks['mlb'] = {'sheet': want[-1]['name'] if want else '', 'url': want[-1]['webViewLink'] if want else '',
                    'games': [g for g in games.values() if keep_date(g)]}


# ================= STEP 2e: college basketball (picks block + ranks) =================
def cbb():
    if not (NOW.month >= 11 or NOW.month <= 3 or (NOW.month == 4 and NOW.day <= 10)):
        picks['cbb'] = {'sheet': '', 'url': '', 'games': []}; return
    files = ls('1P8oaadd8DTQcavJ_zEtN8biHso4bDMVZ')
    def day_file(d, kind, data):
        lab = d.strftime('%b %d')
        return newest(files, lambda n: re.search(rf' CBB {kind} \({lab}\)' + (r' data\.csv$' if data else r'$'), n) is not None)
    days = [(d, day_file(d, 'Picks', True)) for d in (YDAY, TODAY)]
    days = [(d, f) for d, f in days if f]
    if not days:
        picks['cbb'] = {'sheet': '', 'url': '', 'games': []}
        return log('cbb: no picks files for today/yesterday')
    readable = newest([f for f in files if f['mimeType'].endswith('spreadsheet')], lambda n: ' CBB Picks (' in n)
    power = newest(files, lambda n: ' CBB Power (' in n and n.endswith(' data.csv'))
    sfiles = ls('1rdTY7B1p-srAPmahFR_dttjHOlHpTgFB')
    conv = newest(sfiles, lambda n: n == 'dashboard_json.py')
    d = os.path.join(TMP, 'cbb'); os.makedirs(d)
    download(conv, os.path.join(d, 'dashboard_json.py'))   # byte size checked against Drive in download()
    download(power, os.path.join(d, 'power.csv'))
    pdate = title_date(power['name'], TODAY.year)
    args = [sys.executable, '-I', os.path.join(d, 'dashboard_json.py'), os.path.join(d, 'out'), readable['name'], readable['webViewLink'],
            pdate.isoformat(), os.path.join(d, 'power.csv')]
    for dd, f in days:
        args.append(download(f, os.path.join(d, f'picks_{dd}.csv')))
    os.makedirs(os.path.join(d, 'out'))
    run(*args)
    block = json.load(open(os.path.join(d, 'out', 'cbb_block.json')))
    ranks = json.load(open(os.path.join(d, 'out', 'cbb_ranks.json')))
    for g in block.get('games', []):
        if not all(g.get(k) not in (None, '') for k in ('id', 'date', 'away', 'home', 'pick', 'win')):
            raise RuntimeError(f'cbb game missing a field: {g.get("id")}')
    if len(ranks.get('teams', {})) < 300:
        raise RuntimeError(f'cbb_ranks has only {len(ranks.get("teams", {}))} teams')
    picks['cbb'] = block
    emit('cbb_ranks.json', ranks, lambda o, n: o.get('teams') == n.get('teams'))


# ================= STEP 2b: NFL ranks =================
def nfl_ranks():
    if not (NOW.month >= 9 or NOW.month <= 2):
        return log('nfl_ranks: offseason')
    d = os.path.join(TMP, 'nfl'); data = os.path.join(d, 'data'); sc = os.path.join(d, 'scripts'); out = os.path.join(d, 'out')
    for p in (data, sc, out):
        os.makedirs(p)
    REL = 'https://github.com/nflverse/nflverse-data/releases/download'
    fetch(f'{REL}/schedules/games.csv', os.path.join(data, 'games.csv'))
    cur = TODAY.year if NOW.month >= 3 else TODAY.year - 1
    gm = [r for r in csv.DictReader(open(os.path.join(data, 'games.csv'))) if r['season'] == str(cur)]
    played = [r['gameday'] for r in gm if r.get('home_score') not in ('', 'NA', None)]
    old = read_json('nfl_ranks.json')
    if old and played and old.get('through', '') >= max(played) and not FORCE:
        return log('nfl_ranks: no new games since last build, skip')
    files = ls('1gQfHq7PGykw4b6qNyLY-NFHfZEM6MVar')
    for n in ('teams.py', 'model.py', 'ratings_v2.py', 'model_v2.json'):
        f = newest(files, lambda x, n=n: x == n)
        if not f:
            raise RuntimeError(f'{n} not found in NFL scripts')
        download(f, os.path.join(sc, n))
    for n in ('teams.py', 'model.py', 'ratings_v2.py'):
        run(sys.executable, '-m', 'py_compile', os.path.join(sc, n))
    shutil.copy(os.path.join(sc, 'model_v2.json'), os.path.join(out, 'model_v2.json'))
    years = re.search(r'YEARS\s*=\s*(.+)', open(os.path.join(sc, 'model.py')).read())
    yrs = eval(years.group(1), {'range': range, 'list': list}) if years else range(cur - 5, cur + 1)
    for y in yrs:
        fetch(f'{REL}/pbp/play_by_play_{y}.parquet', os.path.join(data, f'play_by_play_{y}.parquet'))
    fetch(f'{REL}/weekly_rosters/roster_weekly_{cur}.parquet', os.path.join(data, f'roster_weekly_{cur}.parquet'))
    reg = [r for r in gm if r['game_type'] == 'REG']
    unplayed = [int(r['week']) for r in reg if r.get('home_score') in ('', 'NA', None)]
    wk = min(unplayed) if unplayed else max(int(r['week']) for r in reg)
    run(sys.executable, '-I', os.path.join(sc, 'teams.py'), data, os.path.join(out, 'teams.csv'), os.path.join(out, 'team_games.csv'),
        f'{cur - 2},{cur - 1},{cur}', cwd=sc)
    run(sys.executable, '-I', os.path.join(sc, 'ratings_v2.py'), data, os.path.join(data, 'games.csv'), out, str(wk), cwd=sc)
    T = list(csv.DictReader(open(os.path.join(out, 'teams.csv'))))
    abbr = 'team' if 'team' in T[0] else next(k for k in T[0] if k.lower() in ('abbr', 'posteam', 'team_abbr'))
    rk = lambda key, rev: {t[abbr]: i + 1 for i, t in enumerate(sorted(T, key=lambda t: float(t[key]), reverse=rev))}
    pw, of, df = rk('AdjNet', True), rk('AdjO_epa', True), rk('AdjD_epa', False)
    teams = {t[abbr]: {'power': pw[t[abbr]], 'rating': round(float(t['AdjNet']), 1), 'off': of[t[abbr]], 'offEpa': round(float(t['AdjO_epa']), 3),
                       'def': df[t[abbr]], 'defEpa': round(float(t['AdjD_epa']), 3), 'ppg': round(float(t['PPG']), 1),
                       'papg': round(float(t['PAPG']), 1), 'g': int(float(t['G']))} for t in T}
    if len(teams) != 32:
        raise RuntimeError(f'expected 32 NFL teams, got {len(teams)}')
    through = max(r['gameday'] for r in gm if r.get('home_score') not in ('', 'NA', None))
    emit('nfl_ranks.json', {'updated': stamp(), 'through': through, 'week': wk, 'source': 'NFL Weekly Model v2 (teams.py + ratings_v2.py)',
                            'n': 32, 'teams': teams}, lambda o, n: o.get('teams') == n['teams'])
    nfl_power_check(teams)


TEAMNICK = {'ARI': 'Cardinals', 'ATL': 'Falcons', 'BAL': 'Ravens', 'BUF': 'Bills', 'CAR': 'Panthers', 'CHI': 'Bears', 'CIN': 'Bengals',
            'CLE': 'Browns', 'DAL': 'Cowboys', 'DEN': 'Broncos', 'DET': 'Lions', 'GB': 'Packers', 'HOU': 'Texans', 'IND': 'Colts',
            'JAX': 'Jaguars', 'KC': 'Chiefs', 'LA': 'Rams', 'LAC': 'Chargers', 'LV': 'Raiders', 'MIA': 'Dolphins', 'MIN': 'Vikings',
            'NE': 'Patriots', 'NO': 'Saints', 'NYG': 'Giants', 'NYJ': 'Jets', 'PHI': 'Eagles', 'PIT': 'Steelers', 'SEA': 'Seahawks',
            'SF': '49ers', 'TB': 'Buccaneers', 'TEN': 'Titans', 'WAS': 'Commanders'}


def nfl_power_check(teams):
    """Compare our power ranks to the '#N in Power Rank' numbers in the newest picks sheet's Why text."""
    by_nick = {TEAMNICK.get(k, k): v['power'] for k, v in teams.items()}
    quoted, diffs = 0, []
    for g in picks.get('nfl', {}).get('games', []):
        m = re.match(r'(.+?) (?:are|is) #(\d+) in Power Rank vs #(\d+)', g.get('why', ''))
        if not m:
            continue
        other = g['away'] if g['pick'] == g['home'] else g['home']
        for team, n in ((g['pick'], int(m.group(2))), (other, int(m.group(3)))):
            if team in by_nick:
                quoted += 1
                diffs.append(abs(by_nick[team] - n))
    if quoted:
        close = sum(1 for x in diffs if x <= 3)
        log(f'nfl power check: {close}/{quoted} within 3 places of the picks sheet')
        if close < quoted * 0.6:
            raise RuntimeError(f'NFL power ranks disagree with the picks sheet ({close}/{quoted} within 3) — check before trusting')


# ================= STEP 2c: NBA ranks =================
def nba_ranks():
    if not (NOW.month >= 10 or NOW.month <= 6):
        return log('nba_ranks: offseason')
    files = ls('1Y8OVacj2Le5uI5PbAOWrpRFp-BiEF5-V')
    f = newest(files, lambda n: ' NBA Power (' in n and n.endswith(' data.csv'))
    if not f:
        return log('nba_ranks: no power file yet')
    asof = title_date(f['name'], TODAY.year)
    old = read_json('nba_ranks.json')
    if old and old.get('asof', '') >= asof.isoformat() and not FORCE:
        return log('nba_ranks: power file not newer, skip')
    rows = list(csv.DictReader(io.StringIO(download(f).decode('utf-8-sig'))))
    fl = lambda v, d=1: None if v in ('', None, 'nan') else round(float(v), d)
    iv = lambda v: None if v in ('', None, 'nan') else int(float(v))
    now_rank = {r['team']: i + 1 for i, r in enumerate(sorted(rows, key=lambda r: float(r['power_now']), reverse=True))}
    teams = {r['team']: {'rank': int(float(r['rank'])), 'power': fl(r['power']), 'powerNow': fl(r['power_now']), 'nowRank': now_rank[r['team']],
                         'form': fl(r.get('U')), 'w': iv(r.get('w')), 'l': iv(r.get('l')), 'pdiff': fl(r.get('pdiff')), 'rankChg': iv(r.get('rank_chg'))}
             for r in rows}
    if len(teams) != 30:
        raise RuntimeError(f'expected 30 NBA teams, got {len(teams)}')
    top = min(teams.values(), key=lambda t: t['rank'])
    if top['power'] != max(t['power'] for t in teams.values()):
        raise RuntimeError('NBA rank 1 does not have the highest power')
    emit('nba_ranks.json', {'updated': stamp(), 'asof': asof.isoformat(), 'file': f['name'], 'source': 'NBA Daily Model v2 (predict.py power.csv)',
                            'n': len(teams), 'teams': teams}, lambda o, n: o.get('teams') == n['teams'] and o.get('asof') == n['asof'])


# ================= STEP 2d: CFB ranks =================
def cfb_ranks():
    if not (NOW.month >= 9 or NOW.month == 1 or (NOW.month == 8 and NOW.day >= 20)):
        return log('cfb_ranks: offseason')
    old = read_json('cfb_ranks.json')
    stale = not old or (TODAY - date.fromisoformat(old.get('through', '2000-01-01'))).days > 8
    if NOW.strftime('%a') not in ('Sun', 'Mon', 'Tue') and not stale and not FORCE:
        return log('cfb_ranks: not a run day, skip')
    season = TODAY.year if NOW.month >= 3 else TODAY.year - 1
    d = os.path.join(TMP, 'cfb'); sc, mf, dat, out = (os.path.join(d, x) for x in ('scripts', 'mf', 'data', 'out'))
    for p in (sc, mf, dat, out):
        os.makedirs(p)
    files = ls('1iwyJB3FGrwlnXjjyGumx-eTHkZh48mbn')
    for n in ('model.py', 'teams.py', 'build_ratings.py', 'fetch_data.sh', 'MANIFEST.sha256'):
        f = newest(files, lambda x, n=n: x == n)
        if not f:
            raise RuntimeError(f'{n} missing from CFB scripts')
        download(f, os.path.join(sc, n))
    man = {}
    for line in open(os.path.join(sc, 'MANIFEST.sha256')):
        p = line.split()
        if len(p) == 2:
            man[p[1].lstrip('*')] = p[0]
    for n in ('model.py', 'teams.py', 'build_ratings.py', 'fetch_data.sh'):
        if n in man and hashlib.sha256(open(os.path.join(sc, n), 'rb').read()).hexdigest() != man[n]:
            raise RuntimeError(f'{n} does not match MANIFEST.sha256')
    pj = newest(ls('10BV3urulJ_KV2E0FfVsaBTbaUj8XzjxC'), lambda x: x == 'params.json')
    download(pj, os.path.join(mf, 'params.json'))
    run('bash', os.path.join(sc, 'fetch_data.sh'), dat, str(season - 4), str(season), cwd=sc)
    run(sys.executable, '-I', os.path.join(sc, 'build_ratings.py'), dat, out, str(season), os.path.join(mf, 'params.json'),
        env={'CFB_DATA': dat}, cwd=sc)
    st = json.load(open(os.path.join(out, 'state.json')))
    through = str(st.get('data_through'))[:10]
    if old and old.get('through') == through and not FORCE:
        return log('cfb_ranks: no new games, keep')
    R = list(csv.DictReader(open(os.path.join(out, 'ratings.csv'))))
    teams = {str(r['id']): {'rank': int(float(r['rank'])), 'rating': round(float(r['rating']), 1), 'team': r['team'], 'abbr': r['abbr'], 'rec': r['rec']} for r in R}
    if len(teams) < 120:
        raise RuntimeError(f'only {len(teams)} CFB teams')
    # task's cross-check: top teams vs the "(#N)" ranks quoted in the newest picks sheet
    by_name = {v['team']: v['rank'] for v in teams.values()}
    q = [(m.group(1), int(m.group(2))) for g in picks.get('cfb', {}).get('games', []) for m in re.finditer(r'([A-Z][\w&.\' ()-]+?) \(#(\d+)\)', g.get('why', ''))]
    q = [(t, n) for t, n in q if t in by_name and n <= 25]
    if q:
        close = sum(1 for t, n in q if abs(by_name[t] - n) <= 5)
        log(f'cfb rank check: {close}/{len(q)} top-25 quotes within 5 places')
        if close < len(q) * 0.6:
            raise RuntimeError('CFB ranks disagree with the picks sheet — not written')
    emit('cfb_ranks.json', {'updated': stamp(), 'through': through, 'source': 'NCAA Football Weekly Model (build_ratings.py)', 'n': len(teams), 'teams': teams},
         lambda o, n: o.get('teams') == n['teams'])


# ================= run =================
step('history', history)
for name, fn in (('NFL picks', pk_nfl), ('NBA picks', pk_nba), ('CFB picks', pk_cfb), ('MLB picks', pk_mlb), ('CBB', cbb)):
    step(name, fn)
existing = read_json('picks.json') or {'leagues': {}}
for lg in ('nfl', 'cfb', 'nba', 'cbb', 'mlb'):  # a failed league keeps what's published
    if lg not in picks:
        picks[lg] = existing['leagues'].get(lg, {'sheet': '', 'url': '', 'games': []})
    for g in picks[lg].get('games', []):
        if not all(g.get(k) not in (None, '') for k in ('date', 'away', 'home', 'pick', 'win')):
            problems.append(f'{lg}: a game is missing date/away/home/pick/win ({g.get("away")} @ {g.get("home")}) — kept published picks')
            picks[lg] = existing['leagues'].get(lg, picks[lg])
            break
new_picks = {'updated': stamp(), 'leagues': {k: picks[k] for k in ('nfl', 'cfb', 'nba', 'cbb', 'mlb')}}

if SHADOW:  # side-by-side report against what the Claude task published
    for lg in new_picks['leagues']:
        a, b = existing['leagues'].get(lg, {}), new_picks['leagues'][lg]
        ka = {(g.get('id') or (g['date'], g['away'], g['home'])): g for g in a.get('games', [])}
        kb = {(g.get('id') or (g['date'], g['away'], g['home'])): g for g in b.get('games', [])}
        same = sum(1 for k in ka if k in kb and ka[k] == kb[k])
        log(f'[compare] {lg}: published {len(ka)} games / built {len(kb)}; identical {same}; sheet {"same" if a.get("sheet") == b.get("sheet") else repr(a.get("sheet")) + " vs " + repr(b.get("sheet"))}')
        for k in list(ka)[:40]:
            if k in kb and ka[k] != kb[k]:
                diff = {f: (ka[k].get(f), kb[k].get(f)) for f in set(ka[k]) | set(kb[k]) if ka[k].get(f) != kb[k].get(f)}
                log(f'   differs {k}: ' + json.dumps(diff, ensure_ascii=False)[:400])
        for k in [k for k in ka if k not in kb][:5]:
            log(f'   only published: {k}')
        for k in [k for k in kb if k not in ka][:5]:
            log(f'   only built: {k}')

step('NFL ranks', nfl_ranks)
step('NBA ranks', nba_ranks)
step('CFB ranks', cfb_ranks)
emit('picks.json', new_picks, lambda o, n: o.get('leagues') == n['leagues'])

log('\nUPDATED: ' + (', '.join(written) or 'nothing'))
for n in notes:
    log(n)
if problems:
    log('PROBLEMS:\n  ' + '\n  '.join(problems))
    sys.exit(1)
