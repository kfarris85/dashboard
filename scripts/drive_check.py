"""One-off access check for the dashboard-bot service account.

Prints only folder labels, OK/FAIL, file counts and permission flags: this repo is
public, so its Actions logs are too. Never print file names or contents here.
env: GOOGLE_SA_JSON (the service account key, from the repo secret)
"""
import os, sys, json
from google.oauth2 import service_account
from google.auth.transport.requests import AuthorizedSession

raw = os.environ.get('GOOGLE_SA_JSON', '').strip()
if not raw:
    print('GOOGLE_SA_JSON secret is missing or empty'); sys.exit(1)
try:
    info = json.loads(raw)
except Exception:
    print('GOOGLE_SA_JSON is not valid JSON (paste the whole .json file contents)'); sys.exit(1)
creds = service_account.Credentials.from_service_account_info(
    info, scopes=['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets'])
s = AuthorizedSession(creds)
print('signed in as', info.get('client_email'))

FOLDERS = {
    'NFL Weekly Model': '1QuzlzT45nOF1cOKCzFFiDAqJJA9n01Ox',
    'NFL scripts': '1gQfHq7PGykw4b6qNyLY-NFHfZEM6MVar',
    'NFL weekly picks': '1tMDXXQCmn09oB-xTy6r4bbZYFbQoN4DK',
    'NBA Daily Model': '1Wr_yhmnMC-KSbVaiPLGytpnFBHsf0IFv',
    'NBA scripts v2': '1HYvK2owChhx_1RRxmdKQy1dLAnM58Tod',
    'NBA daily picks': '1Y8OVacj2Le5uI5PbAOWrpRFp-BiEF5-V',
    'NCAA Football Weekly Model': '1w9slfFFGFmgvdA_BHVqr308U7IqmP6Oa',
    'CFB scripts': '1iwyJB3FGrwlnXjjyGumx-eTHkZh48mbn',
    'CFB model files': '10BV3urulJ_KV2E0FfVsaBTbaUj8XzjxC',
    'CFB weekly picks': '1hXyrZWt0QIPmmdXjPdoHDe5MYlghgqt_',
    'NCAA Basketball Daily Model': '1U5yMu6gVgfL8e02JeKLjKCtqjpC1JYUX',
    'CBB scripts': '1rdTY7B1p-srAPmahFR_dttjHOlHpTgFB',
    'CBB daily picks': '1P8oaadd8DTQcavJ_zEtN8biHso4bDMVZ',
    'MLB Daily Model': '1rrSq-wmOk_2NegJNorrQybqGmkn38BQp',
    'MLB daily picks': '1hcBhJsNAe0mm3QOCxSduk8gXFksoeefs',
    'CLV Tracker': '1nNgr2Cusif40YnDW7oR8pHVtMjeJGwC0',
}
SHEET = '1EZUyTMkSO98ZEse6VXqXLCoAlNa8GXXiZ48s-j_aGao'
DRIVE = 'https://www.googleapis.com/drive/v3/files'
bad = []
for label, fid in FOLDERS.items():
    r = s.get(f'{DRIVE}/{fid}', params={'fields': 'id,mimeType', 'supportsAllDrives': 'true'})
    if r.status_code != 200:
        print(f'FAIL  {label}: not shared with the bot (HTTP {r.status_code})'); bad.append(label); continue
    r = s.get(DRIVE, params={'q': f"'{fid}' in parents and trashed=false", 'fields': 'files(id)', 'pageSize': 1000,
                              'supportsAllDrives': 'true', 'includeItemsFromAllDrives': 'true'})
    n = len(r.json().get('files', [])) if r.ok else -1
    print(f'OK    {label}: {n} items visible')
r = s.get(f'{DRIVE}/{SHEET}', params={'fields': 'capabilities(canEdit)'})
if r.status_code != 200:
    print(f'FAIL  Season Tracker sheet: not shared with the bot (HTTP {r.status_code})'); bad.append('Season Tracker')
else:
    edit = r.json().get('capabilities', {}).get('canEdit')
    v = s.get(f'https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values/Log!A1:A2')
    print(f"{'OK  ' if edit else 'WARN'}  Season Tracker sheet: readable={v.ok}, editor={edit}")
    if not edit: bad.append('Season Tracker (needs Editor)')
print('\nRESULT:', 'all good' if not bad else 'fix sharing for: ' + ', '.join(bad))
sys.exit(1 if bad else 0)
