# Premierium ratings - builds one shared file of Rotten Tomatoes scores (critics and audience) for every well-known movie and show,
# matched by TMDb number, so every Premierium box reads the same file and none of them needs a key.
#
# Each run:
#   1. The list of titles to rate: every movie and show on TMDb with at least MIN_VOTES votes (refreshed weekly, kept in state.json.gz).
#   2. Looks up, on MDBList, the titles never looked up yet (best known first), then the ones due a re-check - as many as today's
#      MDBList allowance leaves room for (it never uses the last SAFETY requests).
#   3. Writes ratings.json.gz for the app and state.json.gz for the next run, and report.txt with what happened.
#
# ratings.json.gz: {"v": 1, "made": unix seconds, "movie": {"<tmdb id>": [critics, audience]}, "tv": {...}} - a score is 0-100, -1 when
# Rotten Tomatoes has none. Only titles with at least one score are listed.
#
# Needs MDBLIST_API_KEY and TMDB_KEY (repository secrets). Standard library only.
import os, sys, json, gzip, time, datetime, urllib.request, urllib.parse, urllib.error

MDB = os.environ.get('MDB_API', 'https://api.mdblist.com')
TMDB = os.environ.get('TMDB_API', 'https://api.themoviedb.org/3')
MIN_VOTES = 10
SAFETY = 25                 # MDBList requests always left unused
BATCH = 200                 # titles asked for in one MDBList request (halved if MDBList refuses that many)
SEED_EVERY_DAYS = 7
MAX_MINUTES = 100           # stop looking up in time to save everything before the job's limit
SAMPLES = [('movie', 238, 'The Godfather'), ('movie', 769, 'GoodFellas'), ('movie', 278, 'The Shawshank Redemption'),
           ('tv', 1398, 'The Sopranos'), ('tv', 1396, 'Breaking Bad')]

T0 = time.time()
MKEY = os.environ.get('MDBLIST_API_KEY', '').strip()
TKEY = os.environ.get('TMDB_KEY', '').strip()
TODAY = int(os.environ.get('RT_TODAY') or time.time() // 86400)
log = []


def say(text):
    print(text, flush=True)
    log.append(text)


def get_json(url, body=None, tries=4):
    """GET (or POST with a JSON body). Returns (status, data, headers); waits and retries on 429 / 5xx / network errors."""
    for attempt in range(tries):
        req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method='GET' if body is None else 'POST',
                                     headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'User-Agent': 'premierium-ratings'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or 'null'), dict(r.headers)
        except urllib.error.HTTPError as e:
            text = e.read().decode(errors='replace')[:300]
            if e.code == 429 or e.code >= 500:
                wait = int(e.headers.get('Retry-After') or 0) or (5 * (attempt + 1))
                time.sleep(min(wait, 60))
                continue
            try:
                return e.code, json.loads(text), dict(e.headers)
            except ValueError:
                return e.code, text, dict(e.headers)
        except Exception as e:
            if attempt == tries - 1:
                return 0, str(e), {}
            time.sleep(5 * (attempt + 1))
    return 429, 'still refused after retries', {}


def hide(text):
    """Never let a key reach the log."""
    text = str(text)
    for k in (MKEY, TKEY):
        if k:
            text = text.replace(k, '***')
    return text


def load(path, empty):
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return empty


def save(path, data):
    with gzip.open(path + '.tmp', 'wt', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(path + '.tmp', path)


def day_of(date):
    try:
        return int(datetime.date.fromisoformat(date[:10]).toordinal() - datetime.date(1970, 1, 1).toordinal())
    except Exception:
        return -1


# ---------------------------------------------------------------- 1. the titles to rate
def seed(kind):
    """Every movie / show on TMDb with MIN_VOTES votes or more: {id: [release day, votes]}. TMDb pages stop at 500, so it goes year by year."""
    out = {}
    path = 'movie' if kind == 'movie' else 'tv'
    year_key = 'primary_release_year' if kind == 'movie' else 'first_air_date_year'
    date_key = 'release_date' if kind == 'movie' else 'first_air_date'
    this_year = datetime.date.today().year
    calls = 0
    for year in range(1900, this_year + 2):
        page, pages = 1, 1
        while page <= min(pages, 500):
            q = urllib.parse.urlencode({'api_key': TKEY, year_key: year, 'vote_count.gte': MIN_VOTES, 'sort_by': 'vote_count.desc',
                                        'include_adult': 'false', 'page': page})
            st, data, _ = get_json('%s/discover/%s?%s' % (TMDB, path, q))
            calls += 1
            if st == 401:
                raise SystemExit('TMDB_KEY was refused by TMDb (401) - check the TMDB_KEY secret')
            if st != 200 or not isinstance(data, dict):
                say('  TMDb %s %d page %d: %s' % (kind, year, page, hide(data)[:120]))
                break
            pages = int(data.get('total_pages') or 0)
            for r in data.get('results') or []:
                if r.get('id'):
                    out[str(r['id'])] = [day_of(r.get(date_key) or ''), int(r.get('vote_count') or 0)]
            page += 1
    say('TMDb %s list: %d titles with %d+ votes (%d requests)' % (kind, len(out), MIN_VOTES, calls))
    return out


# ---------------------------------------------------------------- 2. MDBList
def allowance():
    """(requests allowed today, requests used today) from MDBList, or (None, None) when it does not say."""
    st, data, _ = get_json('%s/user?%s' % (MDB, urllib.parse.urlencode({'apikey': MKEY})))
    if st == 401 or st == 403:
        raise SystemExit('MDBLIST_API_KEY was refused by MDBList (%d) - check the MDBLIST_API_KEY secret' % st)
    if st == 200 and isinstance(data, dict):
        lim, used = data.get('api_requests'), data.get('api_requests_count')
        if isinstance(lim, int) and isinstance(used, int):
            return lim, used
    say('MDBList /user did not give the allowance (%s): %s' % (st, hide(data)[:200]))
    return None, None


sources_seen = set()


def scores(item):
    """(critics, audience) from one MDBList title: Rotten Tomatoes' Tomatometer and audience score, -1 when missing."""
    critics, audience = -1, -1
    for r in item.get('ratings') or []:
        src = str(r.get('source') or '').lower()
        sources_seen.add(src)
        v = r.get('value')
        if v is None:
            v = r.get('score')
        if isinstance(v, str):
            try:
                v = float(v.strip().rstrip('%'))
            except ValueError:
                v = None
        if not isinstance(v, (int, float)) or v < 0 or v > 100:
            continue
        if src == 'tomatoes':
            critics = int(round(v))
        elif src in ('tomatoesaudience', 'popcorn', 'audience'):
            audience = int(round(v))
    return critics, audience


single_get = [False]      # one title at a time goes straight to the plain address once a one-title request was refused


def lookup(kind, ids, batch):
    """MDBList for these TMDb ids: ({id: (critics, audience)}, requests used, batch size that worked). Ids MDBList does not know come back missing."""
    mtype = 'movie' if kind == 'movie' else 'show'
    url = '%s/tmdb/%s?%s' % (MDB, mtype, urllib.parse.urlencode({'apikey': MKEY}))
    if len(ids) == 1 and single_get[0]:
        st, data = 0, 'skipped'
    else:
        st, data, _ = get_json(url, {'ids': [str(i) for i in ids]})
    if st == 200 and isinstance(data, list):
        got = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            tid = (item.get('ids') or {}).get('tmdb')
            if tid:
                got[str(tid)] = scores(item)
        return got, 1, batch
    if st == 200 and isinstance(data, dict) and isinstance(data.get('ratings'), list) and len(ids) == 1:
        return {str(ids[0]): scores(data)}, 1, batch
    if len(ids) == 1:
        # One title at a time: the plain address for one title.
        st2, d2, _ = get_json('%s/tmdb/%s/%s?%s' % (MDB, mtype, ids[0], urllib.parse.urlencode({'apikey': MKEY})))
        n = 1 if single_get[0] else 2
        if st2 == 200 and isinstance(d2, dict):
            single_get[0] = True
            return {str(ids[0]): scores(d2)}, n, batch
        if st2 == 404:
            return {}, n, batch
        return None, n, (st, hide(data)[:120], st2, hide(d2)[:120])
    return None, 1, (st, hide(data)[:200])


# ---------------------------------------------------------------- the run
def main():
    if not MKEY or not TKEY:
        raise SystemExit('a secret is missing: ' + ', '.join(n for n, k in (('MDBLIST_API_KEY', MKEY), ('TMDB_KEY', TKEY)) if not k))
    state = load('state.json.gz', {})
    rated = {k: state.get('rated', {}).get(k, {}) for k in ('movie', 'tv')}      # id -> [critics, audience, day checked]
    wanted = state.get('wanted', {})
    if not wanted.get('movie') or TODAY - int(state.get('seeded', 0)) >= SEED_EVERY_DAYS:
        wanted = {'movie': seed('movie'), 'tv': seed('tv')}
        state['seeded'] = TODAY
    else:
        say('TMDb list from %d day(s) ago: %d movies, %d shows' % (TODAY - int(state['seeded']), len(wanted['movie']), len(wanted['tv'])))

    def due(kind, tid):
        r = rated[kind].get(tid)
        if r is None:
            return True
        age = TODAY - r[2]
        rel = wanted[kind].get(tid, [-1, 0])[0]
        since = TODAY - rel if rel >= 0 else 99999
        if r[0] < 0 and r[1] < 0:
            return age >= (30 if since < 365 else 120)
        return age >= (14 if since < 365 else 90 if since < 3 * 365 else 365)

    # Never looked up first (most votes first), then re-checks (longest ago first). Movies and shows go in separate requests, so each kind
    # is cut into its own chunks, and the chunks are put in order by their first title.
    chunks = []
    for phase in (0, 1):
        for kind in ('movie', 'tv'):
            if phase == 0:
                ids = sorted((-v[1], tid) for tid, v in wanted[kind].items() if tid not in rated[kind])
            else:
                ids = sorted((rated[kind][tid][2], tid) for tid in wanted[kind] if tid in rated[kind] and due(kind, tid))
            for s in range(0, len(ids), BATCH):
                part = ids[s:s + BATCH]
                chunks.append(((phase, part[0][0]), kind, [tid for _, tid in part]))
    chunks.sort(key=lambda c: c[0])
    new_total = sum(len(c[2]) for c in chunks if c[0][0] == 0)
    say('Waiting: %d never looked up, %d due a re-check' % (new_total, sum(len(c[2]) for c in chunks) - new_total))

    lim, used = allowance()
    # The allowance is counted in MDBList's own units. Whether one request for many titles counts once or once per title is measured on
    # the first request; until then (and when MDBList does not say), every title is taken to count once.
    allowed = (lim - used - SAFETY) if lim is not None else 900
    say('MDBList allowance: %s, used today: %s - this run may use %d' % (lim, used, max(allowed, 0)))
    batch, units, per_title, measured, looked, requests, cost_note = BATCH, 0.0, 1.0, lim is None, 0, 0, ''
    pending = [(kind, ids) for _, kind, ids in chunks]
    while pending and units < allowed and time.time() - T0 < MAX_MINUTES * 60:
        kind, ids = pending.pop(0)
        room = int((allowed - units) / per_title) if per_title > 0 else len(ids)
        if room < 1:
            break
        if len(ids) > min(batch, room):
            pending.insert(0, (kind, ids[min(batch, room):]))
            ids = ids[:min(batch, room)]
        got, n, info = lookup(kind, ids, batch)
        requests += n
        if got is None:
            units += 1
            if batch > 1:
                batch = max(1, batch // 4)
                pending.insert(0, (kind, ids))
                say('MDBList refused %d at once (%s) - trying %d at a time' % (len(ids), info, batch))
                continue
            say('MDBList refused a single title (%s) - stopping for today' % (info,))
            break
        for tid in ids:
            c, a = got.get(tid, (-1, -1))
            rated[kind][tid] = [c, a, TODAY]
        looked += len(ids)
        if not measured:
            measured = True
            l2, u2 = allowance()
            if u2 is not None:
                per_title = max(0.0, (u2 - used) / float(len(ids)))
                units = float(u2 - used)
                cost_note = 'one request for %d titles counted %d against the allowance' % (len(ids), u2 - used)
                say('MDBList: ' + cost_note)
                continue
        units += per_title * len(ids) if per_title > 0 else 1
        time.sleep(0.2)
    say('Looked up %d titles in %d MDBList requests (batch size %d)' % (looked, requests, batch))

    state['rated'] = rated
    state['wanted'] = wanted
    save('state.json.gz', state)
    out = {'v': 1, 'made': int(time.time())}
    for kind in ('movie', 'tv'):
        out[kind] = {tid: [r[0], r[1]] for tid, r in rated[kind].items() if r[0] >= 0 or r[1] >= 0}
    save('ratings.json.gz', out)

    lines = ['Premierium ratings - run of %s UTC (took %d:%02d)' % (datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'), (time.time() - T0) // 60, (time.time() - T0) % 60), '']
    for kind, name in (('movie', 'Movies'), ('tv', 'Shows')):
        r = rated[kind]
        lines.append('%s: %d to rate, %d looked up, %d with a critics score, %d with an audience score, %d in the file' % (
            name, len(wanted[kind]), len(r), sum(1 for v in r.values() if v[0] >= 0), sum(1 for v in r.values() if v[1] >= 0), len(out[kind])))
    left = sum(1 for kind in ('movie', 'tv') for tid in wanted[kind] if tid not in rated[kind])
    lines.append('Never looked up yet: %d' % left)
    lines.append('Rating sources MDBList gave: ' + (', '.join(sorted(sources_seen)) or '(none this run)'))
    if cost_note:
        lines.append('Allowance: ' + cost_note)
    lines.append('')
    lines.append('Samples (critics / audience):')
    for kind, tid, name in SAMPLES:
        r = rated[kind].get(str(tid))
        lines.append('  %s: %s' % (name, ('%s / %s' % (r[0], r[1])) if r else 'not looked up yet'))
    lines.append('')
    lines.append('Run log:')
    lines += ['  ' + hide(l) for l in log]
    with open('report.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines[:12]))


if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        msg = hide(e)
        with open('report.txt', 'w', encoding='utf-8') as f:
            f.write('Premierium ratings - run of %s UTC FAILED\n\n%s\n\nRun log:\n%s\n' % (datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'), msg, '\n'.join('  ' + hide(l) for l in log)))
        print('::error::' + msg)
        sys.exit(1)
