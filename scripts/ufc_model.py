"""UFC fight model for the dashboard.

Runs on GitHub Actions (free, no Claude credits). Pulls the upcoming and most
recent UFC cards from ESPN, scores every fight, and writes:

  data/ufc.json          the cards + model picks the dashboard's UFC tab shows
  data/ufc_history.json  one row per picked fight, graded once it's final,
                         with closing-line value (CLV) from pick time to close

Model (stats only, then compared to the DraftKings line ESPN carries):
  record quality (shrunk win %), experience, last-3 form, sig strikes landed
  (all per-minute stats shrunk toward UFC averages by number of UFC fights)
  per min + accuracy, takedowns and sub attempts per 15, reach, age (with an
  extra penalty past 34), and KO rate. Weighted logit, damped and capped 15-85%.
  The weights are hand-set priors, not fitted. Grade it on History before
  trusting it, same as the other models.

Picks lock the first time a fight is published (pick, win %, and the line at
pick time). The line keeps updating until the fight starts; that last pre-fight
price is the close used for CLV.

usage: python3 -I scripts/ufc_model.py <data_dir>
       UFC_FIXTURES=<dir> to read ESPN responses from local files (testing)
"""
import sys, os, re, json, math, time, hashlib, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

CT = ZoneInfo('America/Chicago')
ET = ZoneInfo('America/New_York')
SITE = 'https://site.api.espn.com/apis/site/v2/sports/mma/ufc/scoreboard'
CORE = 'https://sports.core.api.espn.com/v2/sports/mma'
FIX = os.environ.get('UFC_FIXTURES')
DATA = sys.argv[1] if len(sys.argv) > 1 else 'data'
NOW = datetime.now(timezone.utc)

_cache = {}


def get(url, tries=3):
    url = url.replace('sports.core.api.espn.pvt', 'sports.core.api.espn.com').replace('http://', 'https://')
    if url in _cache:
        return _cache[url]
    if FIX:
        fn = os.path.join(FIX, hashlib.md5(url.split('?')[0].encode()).hexdigest() + '.json')
        alt = os.path.join(FIX, re.sub(r'[^A-Za-z0-9]+', '_', url.split('//')[1])[:180] + '.json')
        for f in (alt, fn):
            if os.path.exists(f):
                _cache[url] = json.load(open(f))
                return _cache[url]
        return None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (dashboard ufc model)'})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
            _cache[url] = d
            return d
        except Exception as e:  # 404 = no data for this fighter; others retry
            if getattr(e, 'code', None) == 404:
                return None
            time.sleep(1.5 * (i + 1))
    return None


# ---------- helpers ----------
def rec_parse(s):
    m = re.match(r'(\d+)-(\d+)(?:-(\d+))?', s or '')
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)


def nv(a_amer, b_amer):
    """No-vig probability of side A from two American prices."""
    def imp(x):
        x = float(x)
        return 100 / (x + 100) if x > 0 else -x / (-x + 100)
    try:
        pa, pb = imp(a_amer), imp(b_amer)
        return pa / (pa + pb)
    except Exception:
        return None


def amer_of(o):
    """American price out of ESPN's odds objects ({'american':'+130'} or a number)."""
    if o is None:
        return None
    if isinstance(o, (int, float)):
        return float(o)
    v = o.get('american') or o.get('alternateDisplayValue')
    try:
        return float(str(v).replace('+', '')) if v not in (None, '', 'EVEN') else (100.0 if v == 'EVEN' else None)
    except ValueError:
        return None


def clock_sec(s):
    m = re.match(r'(\d+):(\d+)', s or '')
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def ct_date(iso):
    return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(CT).strftime('%Y-%m-%d')


# ---------- fighter data ----------
def fighter(aid):
    prof = get(f'{CORE}/athletes/{aid}') or {}
    st = get(f'{CORE}/athletes/{aid}/statistics') or {}
    stats = {}
    for cat in ((st.get('splits') or {}).get('categories') or []):
        for s in cat.get('stats') or []:
            stats[s.get('name')] = s.get('value')
    # last 3 fights from the event log (played only)
    last, nufc = [], 0
    log = get(f'{CORE}/athletes/{aid}/eventlog') or {}
    for it in ((log.get('events') or {}).get('items') or []):
        if not it.get('played'):
            continue
        if '/leagues/ufc/' in ((it.get('event') or {}).get('$ref') or ''):
            nufc += 1
        if len(last) == 3:
            continue
        ref = (it.get('competitor') or {}).get('$ref')
        if not ref:
            continue
        c = get(ref) or {}
        w = c.get('winner')
        last.append('W' if w is True else 'L' if w is False else '?')
    has_stats = bool(stats) and any((stats.get(k) or 0) for k in ('strikeLPM', 'strikeAccuracy', 'takedownAvg'))
    # ESPN's per-minute stats swing wildly on 1-2 UFC fights (one 40-second KO = 30 strikes/min).
    # Shrink each toward the UFC average by sample size: n UFC fights counts n / (n + 3).
    w = nufc / (nufc + 3.0)
    def sh(key, avg):
        v = stats.get(key)
        return None if not has_stats or v is None else round(w * v + (1 - w) * avg, 2)
    return {
        'nufc': nufc, 'raw': {k: stats.get(k) for k in ('strikeLPM', 'takedownAvg', 'submissionAvg')} if has_stats else None,
        'age': prof.get('age'), 'reach': prof.get('reach'), 'height': prof.get('height'),
        'hdisp': prof.get('displayHeight'), 'stance': (prof.get('stance') or {}).get('text'),
        'nick': prof.get('nickname'), 'head': (prof.get('headshot') or {}).get('href'),
        'camp': (prof.get('association') or {}).get('name'),
        'lpm': sh('strikeLPM', 3.8),
        'acc': sh('strikeAccuracy', 47.0),
        'td': sh('takedownAvg', 1.4),
        'tdAcc': stats.get('takedownAccuracy') if has_stats else None,
        'sub': sh('submissionAvg', 0.5),
        'ko': None if not has_stats else round(w * ((stats.get('koPercentage') or 0) + (stats.get('tkoPercentage') or 0)) + (1 - w) * 30, 1),
        'dec': None if not has_stats else round(w * (stats.get('decisionPercentage') or 0) + (1 - w) * 45, 1),
        'last3': last,
    }


# ---------- the model ----------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def score(A, B):
    """Return (P(A wins), [(contribution, text-for-A, text-for-B), ...])."""
    parts = []
    wa, la, _ = A['wld']; wb, lb, _ = B['wld']
    qa, qb = (wa + 3) / (wa + la + 6), (wb + 3) / (wb + lb + 6)
    parts.append((2.0 * (qa - qb), f"better record ({wa}-{la} vs {wb}-{lb})", f"better record ({wb}-{lb} vs {wa}-{la})"))
    ea, eb = math.log1p(wa + la), math.log1p(wb + lb)
    parts.append((0.15 * (ea - eb), 'more experienced', 'more experienced'))
    fa = A['last3'].count('W') - A['last3'].count('L'); fb = B['last3'].count('W') - B['last3'].count('L')
    def form(x):
        return f"{x['last3'].count('W')}-{x['last3'].count('L')} in last {len(x['last3'])}"
    parts.append((0.20 * (fa - fb), form(A), form(B)))
    if A['lpm'] is not None and B['lpm'] is not None:
        parts.append((0.12 * clamp(A['lpm'] - B['lpm'], -3, 3), f"lands {A['lpm']:.1f} vs {B['lpm']:.1f} sig strikes/min", f"lands {B['lpm']:.1f} vs {A['lpm']:.1f} sig strikes/min"))
        parts.append((0.01 * clamp((A['acc'] or 0) - (B['acc'] or 0), -20, 20), f"more accurate ({A['acc']:.0f}% vs {B['acc']:.0f}%)", f"more accurate ({B['acc']:.0f}% vs {A['acc']:.0f}%)"))
        parts.append((0.10 * clamp((A['td'] or 0) - (B['td'] or 0), -4, 4), f"wrestling edge ({A['td']:.1f} vs {B['td']:.1f} TD/15)", f"wrestling edge ({B['td']:.1f} vs {A['td']:.1f} TD/15)"))
        parts.append((0.08 * clamp((A['sub'] or 0) - (B['sub'] or 0), -3, 3), 'bigger submission threat', 'bigger submission threat'))
        parts.append((0.004 * clamp((A['ko'] or 0) - (B['ko'] or 0), -60, 60), f"more power ({A['ko']:.0f}% vs {B['ko']:.0f}% KO/TKO)", f"more power ({B['ko']:.0f}% vs {A['ko']:.0f}% KO/TKO)"))
    if A['reach'] and B['reach']:
        d = A['reach'] - B['reach']
        parts.append((0.03 * clamp(d, -6, 6), f'+{abs(d):.0f}" reach', f'+{abs(d):.0f}" reach'))
    if A['age'] and B['age']:
        old = lambda a: max(0, a - 34)
        parts.append((-0.04 * clamp(A['age'] - B['age'], -10, 10) - 0.06 * (old(A['age']) - old(B['age'])),
                      f"younger ({A['age']} vs {B['age']})", f"younger ({B['age']} vs {A['age']})"))
    z = 0.8 * sum(p[0] for p in parts)  # damped: hand-set weights, keep it humble until it's graded
    return clamp(1 / (1 + math.exp(-z)), 0.15, 0.85), parts


def rounds_over(A, B, rounds, line):
    """P(fight goes past `line` rounds)."""
    decs = [x['dec'] for x in (A, B) if x['dec'] is not None]
    pd = clamp((sum(decs) / len(decs) / 100) if decs else 0.45, 0.15, 0.85)
    # finishes are front-loaded: weight how much of the fight is left past the line
    frac = clamp((rounds - line) / rounds, 0, 1) * 0.8
    return pd + (1 - pd) * frac, pd


def comp_odds(eid, cid):
    d = get(f'{CORE}/leagues/ufc/events/{eid}/competitions/{cid}/odds') or {}
    for it in d.get('items') or []:
        h, a = it.get('homeAthleteOdds') or {}, it.get('awayAthleteOdds') or {}
        hid = ((h.get('athlete') or {}).get('$ref') or '').split('/athletes/')[-1].split('?')[0]
        aid = ((a.get('athlete') or {}).get('$ref') or '').split('/athletes/')[-1].split('?')[0]
        hc = amer_of(((h.get('current') or {}).get('moneyLine'))) or amer_of(h.get('moneyLine'))
        ac = amer_of(((a.get('current') or {}).get('moneyLine'))) or amer_of(a.get('moneyLine'))
        ho = amer_of(((h.get('open') or {}).get('moneyLine')))
        ao = amer_of(((a.get('open') or {}).get('moneyLine')))
        cur = it.get('current') or {}
        return {
            'book': (it.get('provider') or {}).get('name'),
            'ml': {hid: hc, aid: ac}, 'open': {hid: ho, aid: ao}, 'ids': (hid, aid),
            'ou': it.get('overUnder'),
            'over': amer_of(cur.get('over')) or it.get('overOdds'),
            'under': amer_of(cur.get('under')) or it.get('underOdds'),
        }
    return None


def fmt_amer(x):
    return None if x is None else ('+' if x > 0 else '') + str(int(x))


# ---------- build ----------
def load_events():
    seen, out = set(), []
    def add(d):
        for ev in (d or {}).get('events') or []:
            if ev['id'] not in seen:
                seen.add(ev['id']); out.append(ev)
    d0 = get(SITE)
    add(d0)
    cal = (((d0 or {}).get('leagues') or [{}])[0].get('calendar') or [])
    for c in cal:
        try:
            st = datetime.fromisoformat(c['startDate'].replace('Z', '+00:00'))
        except Exception:
            continue
        if NOW - timedelta(days=4) <= st <= NOW + timedelta(days=10):
            add(get(SITE + '?dates=' + st.astimezone(ET).strftime('%Y%m%d')))
    return out


def side(c):
    a = c.get('athlete') or {}
    return {
        'id': str(c.get('id')), 'name': a.get('displayName') or a.get('fullName'), 'short': a.get('shortName'),
        'flag': (a.get('flag') or {}).get('href'), 'country': (a.get('flag') or {}).get('alt'),
        'rec': ((c.get('records') or [{}])[0]).get('summary', ''), 'winner': c.get('winner'),
    }


def main():
    os.makedirs(DATA, exist_ok=True)
    hpath = os.path.join(DATA, 'ufc_history.json')
    try:
        hist = json.load(open(hpath))
    except Exception:
        hist = {'rows': []}
    rows = {r['key']: r for r in hist.get('rows', [])}

    events = load_events()
    fights = []
    for ev in events:
        comps = ev.get('competitions') or []
        for i, c in enumerate(comps):
            if len(c.get('competitors') or []) != 2:
                continue
            fights.append((ev, c, i, len(comps)))

    ids = {str(x['id']) for _, c, _, _ in fights for x in c['competitors']}
    with ThreadPoolExecutor(8) as ex:
        F = dict(zip(ids, ex.map(fighter, ids)))
    with ThreadPoolExecutor(8) as ex:
        O = dict(zip([c['id'] for _, c, _, _ in fights], ex.map(lambda f: comp_odds(f[0]['id'], f[1]['id']), fights)))

    cards = {}
    for ev, c, idx, n in fights:
        st = (c.get('status') or {}); stt = st.get('type') or {}
        state = stt.get('state')
        cs = sorted(c['competitors'], key=lambda x: x.get('order', 9))
        A, B = side(cs[0]), side(cs[1])
        rounds = (((c.get('format') or {}).get('regulation') or {}).get('periods')) or 3
        key = 'UFC|' + str(c['id'])
        row = rows.get(key)
        odds = O.get(c['id'])
        f = {'id': str(c['id']), 'date': c.get('date'), 'wc': (c.get('type') or {}).get('abbreviation'),
             'rounds': rounds, 'main': idx == n - 1, 'co': idx == n - 2, 'state': state,
             'a': A, 'b': B}
        if odds:
            f['ml'] = {'a': fmt_amer(odds['ml'].get(A['id'])), 'b': fmt_amer(odds['ml'].get(B['id']))}
            f['book'] = odds['book']
        if A['id'] in F and B['id'] in F:
            fa, fb = dict(F[A['id']]), dict(F[B['id']])
            fa['wld'], fb['wld'] = rec_parse(A['rec']), rec_parse(B['rec'])
            if state == 'post':  # record already includes this fight; back it out
                for x, s in ((fa, A), (fb, B)):
                    w, l, d = x['wld']
                    x['wld'] = (w - 1, l, d) if s['winner'] else (w, l - 1, d) if s['winner'] is False and (A['winner'] or B['winner']) else (w, l, d)
            p, parts = score(fa, fb)
            for k in ('a', 'b'):
                x = fa if k == 'a' else fb
                f[k].update({kk: x.get(kk) for kk in ('nufc', 'age', 'reach', 'hdisp', 'stance', 'nick', 'head', 'camp', 'lpm', 'acc', 'td', 'tdAcc', 'sub', 'ko', 'dec', 'last3')})
            pick_a = p >= 0.5
            pick = A if pick_a else B
            win = round(100 * (p if pick_a else 1 - p), 1)
            vegas = None
            if odds and odds['ml'].get(A['id']) is not None and odds['ml'].get(B['id']) is not None:
                va = nv(odds['ml'][A['id']], odds['ml'][B['id']])
                vegas = round(100 * (va if pick_a else 1 - va), 1)
            agrees = None if vegas is None else vegas >= 50
            edge = None if vegas is None else round(win - vegas, 1)
            conf = 'Strong' if win >= 70 else 'Lean' if win >= 60 else 'Toss-up'
            # value = model likes a side well above its no-vig price (most often an underdog). Flagged, not starred:
            # a stats-only model that strongly disagrees with a sharp MMA market is usually the one that's wrong.
            value = edge is not None and 8 <= edge <= 20 and win >= 58
            best = win >= 65 and agrees is not False and (edge is None or edge >= -5)
            # why: the 3 biggest factors for the pick, plus a risk if one points the other way
            sgn = 1 if pick_a else -1
            pro = sorted([x for x in parts if x[0] * sgn > 0.02], key=lambda x: -abs(x[0]))[:3]
            con = sorted([x for x in parts if x[0] * sgn < -0.05], key=lambda x: -abs(x[0]))[:1]
            why = f"{pick['name'].split(' ')[-1]}: " + ', '.join(x[1] if pick_a else x[2] for x in pro) if pro else 'Close on paper.'
            other = B if pick_a else A
            if con:
                why += f". Risk: {other['name'].split(' ')[-1]} is {con[0][2] if pick_a else con[0][1]}"
            if vegas is not None:
                why += f". Vegas has {pick['name'].split(' ')[-1]} at {vegas:.0f}%" + (' (model sees value)' if value else ' (model disagrees)' if agrees is False else '')
            why += '.'
            ou = None
            if odds and odds.get('ou'):
                po, pdist = rounds_over(fa, fb, rounds, float(odds['ou']))
                mkt = nv(odds['over'], odds['under']) if odds.get('over') and odds.get('under') else None
                ou = {'line': odds['ou'], 'p': round(100 * po), 'dist': round(100 * pdist),
                      'mkt': round(100 * mkt) if mkt else None,
                      'over': fmt_amer(odds.get('over')), 'under': fmt_amer(odds.get('under')),
                      'lean': 'Over' if (po > (mkt or 0.5)) else 'Under'}
            pk = fa if pick_a else fb
            method = 'KO/TKO' if (pk['ko'] or 0) >= 45 else 'Submission' if (pk['sub'] or 0) >= 1.2 else 'Decision' if (pk['dec'] or 0) >= 40 or (ou and ou['dist'] >= 55) else 'Any way'
            f.update({'pick': pick['name'], 'pickId': pick['id'], 'win': win, 'vegas': vegas, 'agrees': agrees,
                      'edge': edge, 'conf': conf, 'best': bool(best), 'value': bool(value), 'why': why, 'ou': ou,
                      'method': method})

        # ---- history: lock at first publish, update close while pre, grade when final ----
        nvp = None
        if odds and f.get('pickId') and odds['ml'].get(A['id']) is not None and odds['ml'].get(B['id']) is not None:
            va = nv(odds['ml'][A['id']], odds['ml'][B['id']])
            nvp = round(100 * (va if f['pickId'] == A['id'] else 1 - va), 1)
        if row is None and state == 'pre' and f.get('pick'):
            row = rows[key] = {'key': key, 'lg': 'UFC', 'season': str(datetime.fromisoformat(c['date'].replace('Z', '+00:00')).year),
                               'date': ct_date(c['date']), 'event': ev.get('name'), 'away': B['name'], 'home': A['name'],
                               'pick': f['pick'], 'pickId': f['pickId'], 'win': f['win'], 'vegas': f['vegas'], 'best': f['best'],
                               'atPick': nvp, 'close': nvp, 'mTotal': (f.get('ou') or {}).get('line'), 'lean': (f.get('ou') or {}).get('lean', ''),
                               'final': '', 'result': '', 'ats': '', 'spreadPick': '', 'ou': '', 'clv': '', 'beat': ''}
        if row is not None:
            if state == 'pre' and nvp is not None:
                row['close'] = nvp
                if row.get('atPick') is None:
                    row['atPick'] = nvp
                if row.get('mTotal') in (None, '') and f.get('ou'):
                    row['mTotal'], row['lean'] = f['ou']['line'], f['ou']['lean']
            if state == 'post' and not row.get('result'):
                win_s = A if A['winner'] else B if B['winner'] else None
                per, sec = st.get('period'), clock_sec(st.get('displayClock'))
                if win_s is None:
                    row['result'] = 'P'; row['final'] = 'Draw / No contest'
                else:
                    row['result'] = '✓' if win_s['id'] == row['pickId'] else '✗'
                    how = 'decision' if (per == rounds and sec == 300) else f'R{per} {st.get("displayClock")}' if per else ''
                    row['final'] = f"{win_s['name']} by {how}".strip()
                if per and sec is not None:
                    act = round((per - 1) + sec / 300, 2)
                    row['aTotal'] = act
                    if row.get('mTotal') not in (None, '') and row.get('lean'):
                        L = float(row['mTotal'])
                        row['ou'] = 'P' if act == L else ('✓' if (act > L) == (row['lean'] == 'Over') else '✗')
                if row.get('atPick') is not None and row.get('close') is not None:
                    d = round(row['close'] - row['atPick'], 1)
                    row['clv'] = d
                    row['beat'] = '✓' if d > 0.5 else '✗' if d < -0.5 else '='
            if state != 'pre':
                f['locked'] = {k: row.get(k) for k in ('pick', 'win', 'vegas', 'best')}
            f['result'] = row.get('result') or None
            f['final'] = row.get('final') or None
        cards.setdefault(ev['id'], {'id': ev['id'], 'name': ev.get('name'), 'date': ev.get('date'),
                                    'venue': ((c.get('venue') or {}).get('fullName')),
                                    'city': ', '.join(v for v in [((c.get('venue') or {}).get('address') or {}).get('city'),
                                                                   ((c.get('venue') or {}).get('address') or {}).get('state') or ((c.get('venue') or {}).get('address') or {}).get('country')] if v),
                                    'tv': ', '.join(sorted({n for b in (c.get('broadcasts') or []) for n in (b.get('names') or [])})),
                                    'fights': []})['fights'].append(f)

    out = {'updated': datetime.now(CT).isoformat(timespec='seconds'),
           'model': 'UFC v1.1: record, experience, form, striking, grappling, reach, age. Hand-set weights, not fitted.',
           'events': sorted(cards.values(), key=lambda e: e['date'] or '')}
    for e in out['events']:
        e['fights'].reverse()  # main event first, like a fight card
    json.dump(out, open(os.path.join(DATA, 'ufc.json'), 'w'), ensure_ascii=False, separators=(',', ':'))
    hist = {'updated': out['updated'], 'source': 'UFC model (GitHub Action)',
            'rows': sorted(rows.values(), key=lambda r: (r['date'], r['key']), reverse=True)}
    json.dump(hist, open(hpath, 'w'), ensure_ascii=False, separators=(',', ':'))
    n = sum(len(e['fights']) for e in out['events'])
    print(f"{len(out['events'])} cards, {n} fights, {sum(1 for e in out['events'] for f in e['fights'] if f.get('best'))} best picks, "
          f"{sum(1 for r in rows.values() if r.get('result') in ('✓', '✗'))} graded in history")


if __name__ == '__main__':
    main()
