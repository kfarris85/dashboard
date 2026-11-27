"""Daily men's college basketball model run (GitHub Actions).

Replaces the Claude scheduled task "Daily NCAA Basketball Model". The model's code stays
in Kyle's Drive ("NCAA Basketball Daily Model" -> scripts) and is downloaded and checked
against MANIFEST.sha256 on every run; it is never copied into this (public) repo.

Each run:
  1. today's slate + the last two days' finals from ESPN
  2. predict.py -> picks + power ratings
  3. double-checks (the ones the Claude task did by hand, done for every game):
     one row per upcoming game; every game's margin and win % recomputed from the power
     ratings (margin = home off - away off + away def - home def + 3.6, 0 at neutral;
     win % = Phi(margin / 10.7)); every ★ follows the rule (agrees with Vegas, Vegas >= 70,
     model >= 60); 300+ rated teams
  4. saves picks / power / line-movement log to data/cbb/ (a robot login can't create
     files in a personal Drive, so these live here now)
  5. grades yesterday (Results), grades the line-movement log against closing lines,
     updates the season ledger
  6. upserts graded games into the Season Tracker Log (same rules as Season Tracker Sync:
     new key -> append, existing key -> fill blank cells only, never overwrite)
  7. phone alert via ntfy only on days with a ★ Best Pick (or if something fails)

usage: python3 -I scripts/cbb_daily.py <repo dir> [--shadow]
  --shadow  run steps 1-3, compare with the Claude run's picks file in Drive, write nothing
env: GOOGLE_SA_JSON, NTFY_TOPIC (optional), DASH_URL
"""
import sys, os, re, io, csv, json, math, time, hashlib, subprocess, tempfile, urllib.request, urllib.parse
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

REPO = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else '.')
SHADOW = '--shadow' in sys.argv
CT = ZoneInfo('America/Chicago')
NOW = datetime.now(CT)
TODAY = NOW.date()
Y1, Y2 = TODAY - timedelta(days=1), TODAY - timedelta(days=2)
SEASON = TODAY.year + 1 if TODAY.month >= 8 else TODAY.year
LABEL = f'{SEASON - 1}-{str(SEASON)[2:]}'
OUT = os.path.join(REPO, 'data', 'cbb')
TMP = tempfile.mkdtemp(prefix='cbb_')
SB = 'https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball'
DAILY_PICKS, SCRIPTS = '1P8oaadd8DTQcavJ_zEtN8biHso4bDMVZ', '1rdTY7B1p-srAPmahFR_dttjHOlHpTgFB'
CLV_FOLDER = '1nNgr2Cusif40YnDW7oR8pHVtMjeJGwC0'
TRACKER = '1EZUyTMkSO98ZEse6VXqXLCoAlNa8GXXiZ48s-j_aGao'
DASH = os.environ.get('DASH_URL', 'https://kfarris85.github.io/dashboard/')
problems, notes = [], []

S = AuthorizedSession(service_account.Credentials.from_service_account_info(
    json.loads(os.environ['GOOGLE_SA_JSON']),
    scopes=['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/spreadsheets']))
DRIVE = 'https://www.googleapis.com/drive/v3/files'


def log(*a):
    print(*a, flush=True)


def mdd(d):
    return d.strftime('%b %d')


def fetch_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (cbb model)'})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if getattr(e, 'code', None) == 404:
                return None
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def fetch_file(url, path):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (cbb model)'})
    with urllib.request.urlopen(req, timeout=300) as r:
        open(path, 'wb').write(r.read())
    return path


def ls(folder):
    out, tok = [], None
    while True:
        p = {'q': f"'{folder}' in parents and trashed=false", 'pageSize': 1000,
             'fields': 'nextPageToken,files(id,name,mimeType,createdTime,size,webViewLink)'}
        if tok:
            p['pageToken'] = tok
        r = S.get(DRIVE, params=p); r.raise_for_status(); j = r.json()
        out += j.get('files', []); tok = j.get('nextPageToken')
        if not tok:
            return out


def newest(files, pred):
    c = [f for f in files if pred(f['name'])]
    return max(c, key=lambda f: f['createdTime']) if c else None


def dl(f):
    r = S.get(f"{DRIVE}/{f['id']}", params={'alt': 'media'}); r.raise_for_status()
    if f.get('size') and int(f['size']) != len(r.content):
        raise RuntimeError(f"{f['name']}: got {len(r.content)} bytes, Drive says {f['size']}")
    return r.content


def sheet_csv(fid):
    r = S.get(f'https://sheets.googleapis.com/v4/spreadsheets/{fid}/values/A1:AZ'); r.raise_for_status()
    v = r.json().get('values', [])
    return [dict(zip(v[0], row + [''] * (len(v[0]) - len(row)))) for row in v[1:]] if v else []


def read_csv_bytes(b):
    return list(csv.DictReader(io.StringIO(b.decode('utf-8-sig'))))


def write_csv(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore'); w.writeheader()
        for r in rows:
            w.writerow({c: ('' if r.get(c) is None else r.get(c)) for c in cols})


def num(v):
    try:
        v = str(v).strip().replace('%', '').replace('+', '')
        return None if v in ('', '—', 'nan', 'None') else float(v)
    except ValueError:
        return None


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def novig(a, b):
    f = lambda m: (100 / (m + 100)) if m > 0 else (-m / (-m + 100))
    try:
        x, y = f(float(a)), f(float(b))
        return x / (x + y)
    except Exception:
        return None


def ntfy(title, body, prio='default', tags=''):
    topic = os.environ.get('NTFY_TOPIC')
    if not topic or SHADOW:
        return
    req = urllib.request.Request(f'https://ntfy.sh/{topic}', data=body.encode('utf-8'), method='POST',
                                 headers={'Title': title.encode('utf-8'), 'Priority': prio, 'Tags': tags, 'Click': DASH, 'Markdown': 'yes'})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        log(f'ntfy failed: {e}')


# ---------- repo-or-Drive lookups (Drive holds everything before the move) ----------
_drive = {}


def drive_files():
    if 'dp' not in _drive:
        _drive['dp'] = ls(DAILY_PICKS)
    return _drive['dp']


def picks_rows(d):
    p = os.path.join(OUT, 'picks', f'{d}.csv')
    if os.path.exists(p):
        return list(csv.DictReader(open(p)))
    f = newest(drive_files(), lambda n: n == f'{LABEL} CBB Picks ({mdd(d)}) data.csv')
    return read_csv_bytes(dl(f)) if f else None


def results_exist(d):
    if os.path.exists(os.path.join(OUT, 'results', f'{d}.csv')):
        return True
    return newest(drive_files(), lambda n: n == f'{LABEL} CBB Results ({mdd(d)})') is not None


# ================= 1-3: slate, predict, checks =================
def scoreboard(d):
    j = fetch_json(f'{SB}/scoreboard?groups=50&limit=400&dates={d.strftime("%Y%m%d")}')
    p = os.path.join(TMP, f'sb_{d}.json'); json.dump(j, open(p, 'w'))
    return j, p


def model_run(sbt, pt, p1, p2):
    pre = [e for e in sbt.get('events', []) if ((e.get('status') or {}).get('type') or {}).get('state') == 'pre']
    sc = os.path.join(TMP, 'scripts'); os.makedirs(sc)
    files = ls(SCRIPTS)
    for n in ('model.py', 'predict.py', 'dashboard_json.py', 'params.json', 'MANIFEST.sha256'):
        f = newest(files, lambda x, n=n: x == n)
        if not f:
            raise RuntimeError(f'{n} missing from the model scripts folder')
        open(os.path.join(sc, n), 'wb').write(dl(f))   # dl() checks byte size against Drive
    man = dict((l.split()[1].lstrip('*'), l.split()[0]) for l in open(os.path.join(sc, 'MANIFEST.sha256')) if len(l.split()) == 2)
    for n in ('model.py', 'predict.py', 'params.json', 'dashboard_json.py'):
        if n in man and hashlib.sha256(open(os.path.join(sc, n), 'rb').read()).hexdigest() != man[n]:
            raise RuntimeError(f'{n} does not match MANIFEST.sha256 — not running a changed script')
    if not pre:
        log('no upcoming games today')
        return None
    data = os.path.join(TMP, 'data'); os.makedirs(data)
    for y in (SEASON, SEASON - 1, SEASON - 2):
        fetch_file(f'https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-data/main/mbb/schedules/parquet/mbb_schedule_{y}.parquet',
                   os.path.join(data, f'mbb_schedule_{y}.parquet'))
    out = os.path.join(TMP, 'out')
    r = subprocess.run([sys.executable, '-I', os.path.join(sc, 'predict.py'), data, os.path.join(sc, 'params.json'), TODAY.isoformat(),
                        pt, out, p1, p2], capture_output=True, text=True)
    log(r.stdout[-800:])
    if r.returncode:
        raise RuntimeError('predict.py failed: ' + r.stderr[-500:])
    picks = list(csv.DictReader(open(os.path.join(out, 'picks.csv'))))
    power = list(csv.DictReader(open(os.path.join(out, 'power.csv'))))
    check(picks, power, pre, json.load(open(os.path.join(sc, 'params.json'))))
    return picks, power, out


def check(picks, power, pre, params):
    hca, sig = params.get('HCA', 3.6), params.get('SIGMA', 10.7)
    if len(power) < 300:
        raise RuntimeError(f'only {len(power)} rated teams')
    if len(picks) != len(pre):
        raise RuntimeError(f'{len(picks)} picks for {len(pre)} upcoming games')
    by_abbr = {}
    for t in power:
        by_abbr.setdefault(t['abbr'], []).append(t)
    bad, checked = [], 0
    for p in picks:
        h, a = by_abbr.get(p['Home']), by_abbr.get(p['Away'])
        if h and a and len(h) == 1 and len(a) == 1:   # abbreviations can repeat for tiny schools; skip those
            h, a = h[0], a[0]
            m = float(h['off']) - float(a['off']) + float(a['def']) - float(h['def']) + (0 if p['Neutral'] == 'Y' else hca)
            pm = float(p['Proj Home']) - float(p['Proj Away'])
            wp = phi(abs(m) / sig) * 100
            pick_home = m >= 0
            if abs(m - pm) > 0.35 or abs(wp - float(p['Win %'])) > 0.6 or (p['Probable Winner'] == p['Home']) != pick_home and abs(m) > 0.3:
                bad.append(f"{p['Away']}@{p['Home']}: recomputed {m:+.1f}/{wp:.1f}% vs sheet {pm:+.1f}/{p['Win %']}%")
            checked += 1
        v, w = num(p['Vegas Win % (pick)']), num(p['Win %'])
        rule = p['Agrees w/ Vegas'] == 'Yes' and v is not None and v >= 70 and w >= 60
        if rule != (p['Best Pick'] == '★'):
            bad.append(f"{p['Away']}@{p['Home']}: ★ flag doesn't follow the rule")
    log(f'double-check: {checked} games recomputed from power ratings, {len(picks)} ★ rules checked, {len(bad)} problems')
    if checked < len(picks) * 0.7:
        raise RuntimeError(f'could only recompute {checked} of {len(picks)} games')
    if bad:
        raise RuntimeError('model check failed: ' + '; '.join(bad[:4]))


# ================= 4b: line-movement (CLV) log =================
CLV_COLS = ['league', 'season', 'game_key', 'game_date', 'kickoff_ct', 'away', 'home', 'run', 'captured_at_ct', 'line_source', 'model_pick',
            'model_win_pct', 'best_pick', 'side', 'side_spread_at_pick', 'side_ml_at_pick', 'side_novig_at_pick', 'model_edge']
CLV_RES = CLV_COLS + ['close_source', 'side_spread_close', 'side_ml_close', 'side_novig_close', 'clv_pts', 'clv_prob', 'beat_close', 'final', 'side_result']


def clv_rows(picks, captured):
    out = []
    for p in picks:
        sp = (p.get('Spread Pick') or '').strip()
        m = re.match(r'^(.*\S)\s+([+-]?\d+(?:\.\d+)?)$', sp)
        if not m:
            continue
        side, n = m.group(1), float(m.group(2))
        home = side == p['Home']
        ml_s, ml_o = (p['Home ML'], p['Away ML']) if home else (p['Away ML'], p['Home ML'])
        nvp = novig(ml_s, ml_o) if ml_s not in ('', None) and ml_o not in ('', None) else None
        ps, po = (float(p['Proj Home']), float(p['Proj Away'])) if home else (float(p['Proj Away']), float(p['Proj Home']))
        out.append({'league': 'NCAAB', 'season': LABEL, 'game_key': p['Game ID'], 'game_date': p['Date'], 'kickoff_ct': p['Tip (CT)'],
                    'away': p['Away'], 'home': p['Home'], 'run': 'am', 'captured_at_ct': captured, 'line_source': 'ESPN scoreboard',
                    'model_pick': p['Probable Winner'], 'model_win_pct': p['Win %'], 'best_pick': p['Best Pick'], 'side': side,
                    'side_spread_at_pick': f'{n:g}', 'side_ml_at_pick': ml_s, 'side_novig_at_pick': '' if nvp is None else round(nvp * 100, 1),
                    'model_edge': round(ps - po + n, 1)})
    return out


# ================= 5: grade yesterday =================
RES_COLS = ['Game ID', 'Date', 'Away', 'Home', 'Pick', 'Win %', 'Vegas %', 'Best Pick', 'Final', 'Result', 'Spread Pick', 'ATS',
            'Model Total', 'Market Total', 'Actual Total', 'Total Lean', 'O/U Result']


def finals(*paths):
    out = {}
    for p in paths:
        for e in (json.load(open(p)) or {}).get('events', []):
            st = (e.get('status') or {}).get('type') or {}
            if not st.get('completed'):
                continue
            c = (e.get('competitions') or [{}])[0]
            cs = {x.get('homeAway'): x for x in c.get('competitors', [])}
            if 'home' in cs and 'away' in cs:
                out[str(e['id'])] = (cs['away']['team'].get('abbreviation'), float(cs['away'].get('score') or 0),
                                     cs['home']['team'].get('abbreviation'), float(cs['home'].get('score') or 0))
    return out


def grade(picks, F):
    rows = []
    for p in picks:
        f = F.get(str(p['Game ID']))
        if not f:
            continue
        aa, as_, ha, hs = f
        win = p['Home'] if hs > as_ else p['Away']
        r = {'Game ID': p['Game ID'], 'Date': p['Date'], 'Away': p['Away'], 'Home': p['Home'], 'Pick': p['Probable Winner'],
             'Win %': p['Win %'], 'Vegas %': p.get('Vegas Win % (pick)', ''), 'Best Pick': p['Best Pick'],
             'Final': f"{p['Away']} {int(as_)}–{p['Home']} {int(hs)}", 'Result': '✓' if p['Probable Winner'] == win else '✗',
             'Spread Pick': p.get('Spread Pick', ''), 'ATS': '', 'Model Total': p.get('Model Total', ''), 'Market Total': p.get('Market Total', ''),
             'Actual Total': int(as_ + hs), 'Total Lean': p.get('Total Lean', ''), 'O/U Result': ''}
        m = re.match(r'^(.*\S)\s+([+-]?\d+(?:\.\d+)?)$', (p.get('Spread Pick') or '').strip())
        if m:
            my, op = (hs, as_) if m.group(1) == p['Home'] else (as_, hs)
            d = my + float(m.group(2)) - op
            r['ATS'] = '✓' if d > 0 else '✗' if d < 0 else 'P'
        mt, lean = num(p.get('Market Total')), (p.get('Total Lean') or '')
        if mt is not None and lean in ('Over', 'Under'):
            t = as_ + hs
            r['O/U Result'] = 'P' if t == mt else ('✓' if (t > mt) == (lean == 'Over') else '✗')
        rows.append(r)
    return rows


# ================= 5a/5b: ledger + CLV grading =================
BANDS = [('50-59', 50, 60), ('60-69', 60, 70), ('70-79', 70, 80), ('80-89', 80, 90), ('90+', 90, 101)]


def empty_ledger():
    z = lambda: {'n': 0, 'beat': 0, 'lost': 0, 'flat': 0, 'sum_clv_pts': 0, 'side': [0, 0]}
    c = z(); c['files'] = []; c['edge3'] = z()
    return {'season': LABEL, 'dates': [], 'all': [0, 0], 'best': [0, 0], 'ats': [0, 0], 'bands': {b[0]: [0, 0] for b in BANDS},
            'last': {}, 'clv': c}


def load_ledger():
    p = os.path.join(OUT, 'record.json')
    if os.path.exists(p):
        return json.load(open(p))
    f = newest(drive_files(), lambda n: n.startswith(f'{LABEL} CBB Record (') and n.endswith(' data.json'))
    return json.loads(dl(f)) if f else None


def add_results(L, rows, day):
    if day.isoformat() in L['dates']:
        return
    i = lambda ok: 0 if ok else 1
    day_all, day_best = [0, 0], [0, 0]
    for r in rows:
        ok = r['Result'] == '✓'
        L['all'][i(ok)] += 1; day_all[i(ok)] += 1
        if r['Best Pick'] == '★':
            L['best'][i(ok)] += 1; day_best[i(ok)] += 1
        if r['ATS'] in ('✓', '✗'):
            L['ats'][i(r['ATS'] == '✓')] += 1
        w = num(r['Win %']) or 0
        for b, lo, hi in BANDS:
            if lo <= w < hi:
                L['bands'][b][i(ok)] += 1
    L['dates'].append(day.isoformat()); L['dates'].sort()
    if not L.get('last') or day.isoformat() >= L.get('_lastday', ''):
        L['last'] = {'label': mdd(day), 'all': day_all, 'best': day_best}; L['_lastday'] = day.isoformat()


def close_line(gid):
    j = fetch_json(f'https://raw.githubusercontent.com/sportsdataverse/hoopR-mbb-raw/main/mbb/json/final/{gid}.json')
    src = 'hoopR-mbb-raw pickcenter'
    if not j or not j.get('pickcenter'):
        j = fetch_json(f'{SB}/summary?event={gid}'); src = 'ESPN summary pickcenter'
    pc = ((j or {}).get('pickcenter') or [None])[0]
    if not pc:
        return None
    hdr = (((j.get('header') or {}).get('competitions') or [{}])[0].get('competitors') or [])
    home = next((c['team']['abbreviation'] for c in hdr if c.get('homeAway') == 'home'), None)
    away = next((c['team']['abbreviation'] for c in hdr if c.get('homeAway') == 'away'), None)
    det = str(pc.get('details') or '')
    hs = None
    if re.match(r'^(EVEN|PK|PICK)', det, re.I):
        hs = 0.0
    else:
        m = re.match(r'^(.*\S)\s+([+-]?\d+(?:\.\d+)?)$', det)
        if m:
            hs = float(m.group(2)) if m.group(1) == home else (-float(m.group(2)) if m.group(1) == away else None)
    if hs is None and pc.get('spread') is not None:
        hs = float(pc['spread'])
    hml = ((pc.get('homeTeamOdds') or {}).get('moneyLine')); aml = ((pc.get('awayTeamOdds') or {}).get('moneyLine'))
    return {'src': src, 'home': home, 'away': away, 'hs': hs, 'hml': hml, 'aml': aml}


def grade_clv(L, F, captured_files):
    """captured_files: {date: rows} of CLV picks logs from the last 4 days without results yet."""
    out = {}
    for d, rows in captured_files.items():
        if not all(str(r['game_key']) in F for r in rows):
            continue   # wait until that slate is all final
        res = []
        for r in rows:
            a_s, a_sc, h_s, h_sc = F[str(r['game_key'])]
            cl = close_line(r['game_key'])
            x = dict(r)
            home_side = r['side'] == r['home']
            my, op = (h_sc, a_sc) if home_side else (a_sc, h_sc)
            pick_n = float(r['side_spread_at_pick'])
            dd = my + pick_n - op
            x['side_result'] = '✓' if dd > 0 else '✗' if dd < 0 else 'P'
            x['final'] = f"{r['away']} {int(a_sc)}–{r['home']} {int(h_sc)}"
            if cl and cl['hs'] is not None:
                sc = cl['hs'] if home_side else -cl['hs']
                ml_s, ml_o = (cl['hml'], cl['aml']) if home_side else (cl['aml'], cl['hml'])
                nvc = novig(ml_s, ml_o) if ml_s and ml_o else None
                x.update({'close_source': cl['src'], 'side_spread_close': f'{sc:g}', 'side_ml_close': ml_s or '',
                          'side_novig_close': '' if nvc is None else round(nvc * 100, 1)})
                clv = round(pick_n - sc, 1)
                x['clv_pts'] = clv
                x['clv_prob'] = '' if nvc is None or r['side_novig_at_pick'] in ('', None) else round(nvc * 100 - float(r['side_novig_at_pick']), 1)
                x['beat_close'] = '✓' if clv > 0 else '=' if clv == 0 else '✗'
            res.append(x)
        title = f'NCAAB CLV Results ({d}) data.csv'
        if title not in L['clv']['files']:
            for x in res:
                if x.get('clv_pts') in (None, ''):
                    continue
                for bucket in [L['clv']] + ([L['clv']['edge3']] if float(x['model_edge']) >= 3 else []):
                    bucket['n'] += 1
                    bucket['beat' if x['beat_close'] == '✓' else 'lost' if x['beat_close'] == '✗' else 'flat'] += 1
                    bucket['sum_clv_pts'] = round(bucket['sum_clv_pts'] + float(x['clv_pts']), 1)
                    if x['side_result'] in ('✓', '✗'):
                        bucket['side'][0 if x['side_result'] == '✓' else 1] += 1
            L['clv']['files'].append(title)
        out[d] = res
    return out


def clv_line(c):
    if not c.get('n'):
        return ''
    bl = c['beat'] + c['lost']
    v = 'too early to read' if c['n'] < 300 else ('possible edge — worth a closer look' if c['sum_clv_pts'] / c['n'] > 0.5 and bl and c['beat'] / bl >= .55 else 'no edge showing')
    return (f"CLV tracker: {c['n']} graded · beat close {100 * c['beat'] / bl:.0f}% · avg {c['sum_clv_pts'] / c['n']:+.2f} pts · "
            f"at our number {c['side'][0]}–{c['side'][1]} · {v}") if bl else ''


# ================= 6: Season Tracker Log upsert =================
def tracker_upsert(results_by_day, clv_by_day):
    rows = {}
    for d, rs in results_by_day.items():
        src = f'{LABEL} CBB Results ({mdd(date.fromisoformat(d) if isinstance(d, str) else d)})'
        for r in rs:
            rows['NCAAB|' + str(r['Game ID'])] = ['NCAAB|' + str(r['Game ID']), 'NCAAB', LABEL, r['Date'], r['Away'], r['Home'], r['Pick'],
                                                  r['Win %'], r['Vegas %'], r['Best Pick'], r['Final'], r['Result'], r['Spread Pick'], r['ATS'],
                                                  r['Model Total'], r['Market Total'], r['Actual Total'], r['Total Lean'], r['O/U Result'], '', '', src]
    clv = {('NCAAB|' + str(x['game_key'])): (x.get('clv_pts', ''), x.get('beat_close', '')) for rs in clv_by_day.values() for x in rs}
    if not rows and not clv:
        return 0, 0
    r = S.get(f'https://sheets.googleapis.com/v4/spreadsheets/{TRACKER}/values/Log!A1:V'); r.raise_for_status()
    log_rows = r.json().get('values', [])
    where = {row[0]: i + 1 for i, row in enumerate(log_rows) if row}
    appends, updates = [], []
    for k, v in rows.items():
        if k in clv:
            v[19], v[20] = clv[k]
        if k not in where:
            appends.append([str(x) for x in v])
            continue
        cur = log_rows[where[k] - 1] + [''] * 22
        for ci, val in enumerate(v):   # fill blank cells only, never overwrite
            if str(cur[ci]).strip() == '' and str(val).strip() != '':
                updates.append({'range': f'Log!{chr(65 + ci)}{where[k]}', 'values': [[str(val)]]})
    for k, (c, b) in clv.items():
        if k in where and k not in rows:
            cur = log_rows[where[k] - 1] + [''] * 22
            for ci, val in ((19, c), (20, b)):
                if str(cur[ci]).strip() == '' and str(val).strip() != '':
                    updates.append({'range': f'Log!{chr(65 + ci)}{where[k]}', 'values': [[str(val)]]})
    if len(set(where)) != len([row for row in log_rows if row and row[0]]):
        raise RuntimeError('Season Tracker Log already has duplicate keys — not writing')
    if appends:
        r = S.post(f'https://sheets.googleapis.com/v4/spreadsheets/{TRACKER}/values/Log!A:V:append',
                   params={'valueInputOption': 'RAW', 'insertDataOption': 'INSERT_ROWS'}, json={'values': appends})
        if r.status_code == 403:
            raise RuntimeError('Season Tracker is shared as Viewer — share it with the bot as Editor so graded games reach the History tab')
        r.raise_for_status()
    if updates:
        r = S.post(f'https://sheets.googleapis.com/v4/spreadsheets/{TRACKER}/values:batchUpdate', json={'valueInputOption': 'RAW', 'data': updates})
        if r.status_code == 403:
            raise RuntimeError('Season Tracker is shared as Viewer — share it with the bot as Editor')
        r.raise_for_status()
    return len(appends), len(updates)


# ================= run =================
def main():
    if not (TODAY.month >= 11 or TODAY.month <= 3 or (TODAY.month == 4 and TODAY.day <= 10)):
        return log('offseason')
    captured = NOW.strftime('%Y-%m-%d %H:%M')
    sbt, pt = scoreboard(TODAY)
    _, p1 = scoreboard(Y1)
    _, p2 = scoreboard(Y2)
    try:
        res = model_run(sbt, pt, p1, p2)
    except Exception as e:   # a failed model check publishes no picks, but grading still runs
        problems.append(f'today\'s picks NOT published: {e}')
        res = None
    if SHADOW:
        if res:
            picks = res[0]
            f = newest(drive_files(), lambda n: n == f'{LABEL} CBB Picks ({mdd(TODAY)}) data.csv')
            if f:
                theirs = {r['Game ID']: r for r in read_csv_bytes(dl(f))}
                both = [p for p in picks if p['Game ID'] in theirs]
                same = [p for p in both if all(p[c] == theirs[p['Game ID']][c] for c in ('Proj Away', 'Proj Home', 'Probable Winner', 'Win %'))]
                log(f'[compare] Claude run had {len(theirs)} games, this run {len(picks)} (games already started drop off); '
                    f'{len(both)} in both; model numbers identical in {len(same)}')
                for p in [p for p in both if p not in same][:5]:
                    t = theirs[p['Game ID']]
                    log(f"   {p['Away']}@{p['Home']}: {t['Proj Away']}-{t['Proj Home']} {t['Win %']}% -> {p['Proj Away']}-{p['Proj Home']} {p['Win %']}%")
        return
    os.makedirs(OUT, exist_ok=True)
    best = []
    if res:
        picks, power, out = res
        cols = list(picks[0].keys()) if picks else []
        write_csv(os.path.join(OUT, 'picks', f'{TODAY}.csv'), picks, cols)
        write_csv(os.path.join(OUT, 'power', f'{TODAY}.csv'), power, list(power[0].keys()))
        write_csv(os.path.join(OUT, 'clv', f'picks_{TODAY}.csv'), clv_rows(picks, captured), CLV_COLS)
        best = [p for p in picks if p['Best Pick'] == '★']
        log(f'saved {len(picks)} picks ({len(best)} ★)')
    F = finals(p1, p2)
    L = load_ledger() or empty_ledger()
    if TODAY.day == 1:
        notes.append('1st of the month: ledger kept incremental (repo results are the full record now)')
    graded = {}
    yp = picks_rows(Y1)
    if yp and not results_exist(Y1):
        g = grade(yp, F)
        if g:
            write_csv(os.path.join(OUT, 'results', f'{Y1}.csv'), g, RES_COLS)
            add_results(L, g, Y1); graded[Y1.isoformat()] = g
            log(f'graded {Y1}: {sum(r["Result"] == "✓" for r in g)}-{sum(r["Result"] == "✗" for r in g)}')
    # CLV logs from the last 4 days with no results file yet (repo first, then Drive)
    pending = {}
    clv_drive = None
    for k in range(1, 5):
        d = (TODAY - timedelta(days=k)).isoformat()
        if os.path.exists(os.path.join(OUT, 'clv', f'results_{d}.csv')):
            continue
        p = os.path.join(OUT, 'clv', f'picks_{d}.csv')
        if os.path.exists(p):
            pending[d] = list(csv.DictReader(open(p)))
            continue
        clv_drive = clv_drive if clv_drive is not None else ls(CLV_FOLDER)
        if newest(clv_drive, lambda n: n == f'NCAAB CLV Results ({d}) data.csv'):
            continue
        f = newest(clv_drive, lambda n: n == f'NCAAB CLV Picks ({d}) data.csv')
        if f:
            pending[d] = read_csv_bytes(dl(f))
    # older slates' finals for CLV grading
    extra = [os.path.join(TMP, f'sb_{d}.json') for d in pending if d not in (Y1.isoformat(), Y2.isoformat())]
    for d in pending:
        if d not in (Y1.isoformat(), Y2.isoformat()):
            scoreboard(date.fromisoformat(d))
    F2 = finals(p1, p2, *[x for x in extra if os.path.exists(x)])
    clv_done = grade_clv(L, F2, pending)
    for d, rs in clv_done.items():
        write_csv(os.path.join(OUT, 'clv', f'results_{d}.csv'), rs, CLV_RES)
    L.pop('_lastday', None)
    json.dump(L, open(os.path.join(OUT, 'record.json'), 'w'), ensure_ascii=False, indent=1)
    try:
        a, u = tracker_upsert(graded, clv_done)
        log(f'Season Tracker: {a} rows added, {u} cells filled')
    except Exception as e:
        problems.append(f'Season Tracker: {e}')
    # phone alert only on ★ days
    if best:
        top = sorted(best, key=lambda p: -float(p['Win %']))
        lines = [f"{p['Tip (CT)']} · {p['Away']} @ {p['Home']} → **{p['Probable Winner']}** {p['Win %']}% (Vegas {p['Vegas Win % (pick)']}%, {p['Line']})" for p in top[:12]]
        more = f'\n+{len(top) - 12} more on the dashboard' if len(top) > 12 else ''
        yl = L.get('last') or {}
        rec = (f"\nYesterday: {yl.get('all', [0, 0])[0]}–{yl.get('all', [0, 0])[1]} (★ {yl.get('best', [0, 0])[0]}–{yl.get('best', [0, 0])[1]}) · "
               f"Season: {L['all'][0]}–{L['all'][1]} (★ {L['best'][0]}–{L['best'][1]}, ATS {L['ats'][0]}–{L['ats'][1]})") if L['all'][0] else ''
        cl = clv_line(L['clv'])
        ntfy(f"College hoops — {NOW.strftime('%a %b %-d')}: {len(best)} ★",
             '\n'.join(lines) + more + rec + (('\n' + cl) if cl else '') + '\nStatistical model, not betting advice.', tags='basketball')


try:
    main()
except Exception as e:
    problems.append(str(e))
for n in notes:
    log(n)
if problems:
    log('PROBLEMS:\n  ' + '\n  '.join(problems))
    ntfy('College hoops model: run failed', '\n'.join(problems)[:900], prio='high', tags='warning')
    sys.exit(1)
