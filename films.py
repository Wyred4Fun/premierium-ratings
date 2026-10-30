# Premierium films - one shared file of every well-known film's running time and release year (and the cast of films that share a name with another),
# so a Premierium box can tell which film a TV program is - and its year - by itself, with no lookup of its own (TmdbThemes.onAirYear).
#
# The list of films is the ratings job's (state.json.gz in premierium-ratings' "latest" release: every movie on TMDb with 10+ votes, with its
# release day and name). Each run:
#   1. Looks up, on TMDb, the running time of the films never looked up yet (best known first), and the cast of those whose name another film
#      shares (the box picks between them by the cast the guide's description names), then re-checks old ones with no running time.
#   2. Writes film_index.json.gz for the app and films_state.json.gz for the next run (both in this repository's "films" release), and
#      films_report.txt with what happened.
#
# film_index.json.gz: {"v": 1, "made": unix seconds, "films": {"<plain name>": [[year, minutes, ["cast", ...]], ...]}}
# The plain name is the title in lower case with accents dropped and only letters and digits kept (as the app compares names). The cast (TMDb's
# first ten names) is there only for a name two or more films share. A film whose running time TMDb does not know is left out.
#
# Needs TMDB_KEY (a repository secret, the ratings job's). Standard library only.
import os, sys, json, gzip, time, datetime, threading, unicodedata, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

TMDB = os.environ.get('TMDB_API', 'https://api.themoviedb.org/3')
TKEY = os.environ.get('TMDB_KEY', '').strip()
TODAY = int(os.environ.get('FILMS_TODAY') or time.time() // 86400)
MAX_MINUTES = float(os.environ.get('FILMS_MAX_MINUTES') or 90)   # stop looking up in time to save everything before the job's limit
WORKERS = 6                     # TMDb requests at once (TMDb allows about 50 a second; this stays well under)
RECHECK_DAYS = 30               # a film with no running time yet is asked again after this long
SAMPLES = ['clambake', 'thething', 'casablanca']
T0 = time.time()
log = []
lock = threading.Lock()


def say(text):
    print(text, flush=True)
    log.append(text)


def hide(text):
    """Never let the key reach the log."""
    text = str(text)
    return text.replace(TKEY, '***') if TKEY else text


def plain(name):
    """The name as the app compares names: lower case, no accents, letters and digits only."""
    s = unicodedata.normalize('NFKD', str(name)).lower()
    return ''.join(c for c in s if c.isalnum() and not unicodedata.combining(c))


def load(path, empty):
    try:
        with gzip.open(path, 'rt', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return empty


def save(path, data):
    with gzip.open(path + '.tmp', 'wt', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'), ensure_ascii=False)
    os.replace(path + '.tmp', path)


def year_of(day):
    """The year of release day [day] (days since 1970-01-01); 0 when not known (the ratings job keeps -1 for a film with no release date - review)."""
    try:
        return (datetime.date(1970, 1, 1) + datetime.timedelta(days=int(day))).year if -40000 < int(day) and int(day) != -1 else 0
    except Exception:
        return 0


def get_json(url, tries=4):
    """GET; returns (status, data). Waits and retries on 429 / 5xx / network errors."""
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'premierium-films'})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read().decode() or 'null')
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                try:
                    wait = int(e.headers.get('Retry-After') or 0)
                except (TypeError, ValueError):
                    wait = 0             # (review) a Retry-After given as a date, not seconds
                time.sleep(min(wait or 2 * (attempt + 1), 30))
                continue
            return e.code, None
        except Exception:
            if attempt == tries - 1:
                return 0, None
            time.sleep(3 * (attempt + 1))
    return 429, None


def look_up(tid, with_cast):
    """(minutes, cast or None) of film [tid] from TMDb; minutes 0 when TMDb has none; None when TMDb could not be asked."""
    q = {'api_key': TKEY}
    if with_cast:
        q['append_to_response'] = 'credits'
    st, data = get_json('%s/movie/%s?%s' % (TMDB, tid, urllib.parse.urlencode(q)))
    if st == 401:
        raise SystemExit('TMDB_KEY was refused by TMDb (401) - check the TMDB_KEY secret')
    if st == 404:
        return 0, None
    if st != 200 or not isinstance(data, dict):
        return None
    try:
        minutes = int(data.get('runtime') or 0)
    except (TypeError, ValueError):
        minutes = 0
    cast = None
    if with_cast:
        people = ((data.get('credits') or {}).get('cast') or [])[:10]
        cast = [str(p.get('name') or '') for p in people if len(str(p.get('name') or '')) >= 4]
    return minutes, cast


def main():
    if not TKEY:
        raise SystemExit('a secret is missing: TMDB_KEY')
    wanted = load('state.json.gz', {}).get('wanted', {}).get('movie', {})      # id -> [release day, votes, name] (the ratings job's list)
    if not wanted:
        raise SystemExit('no film list: state.json.gz from premierium-ratings was not found or is empty')
    state = load('films_state.json.gz', {})
    films = state.get('films', {})        # id -> [minutes, cast or null, day checked]
    say('Film list from the ratings job: %d films; looked up before: %d' % (len(wanted), len(films)))

    # The names two or more films share: their cast is wanted too.
    by_name = {}
    for tid, w in wanted.items():
        p = plain(w[2] if len(w) > 2 else '')
        if p:
            by_name.setdefault(p, []).append(tid)
    shared = {tid for ids in by_name.values() if len(ids) > 1 for tid in ids}
    say('Names shared by two or more films: %d (%d films)' % (sum(1 for ids in by_name.values() if len(ids) > 1), len(shared)))

    def due(tid):
        f = films.get(tid)
        if f is None:
            return True
        if tid in shared and f[1] is None and f[0] > 0:
            return True                     # its name came to be shared since it was looked up: the cast is wanted now
        return f[0] <= 0 and TODAY - int(f[2]) >= RECHECK_DAYS
    todo = sorted((tid for tid in wanted if due(tid)), key=lambda t: -int(wanted[t][1] if len(wanted[t]) > 1 else 0))
    say('To look up this run: %d' % len(todo))

    asked = [0]
    failed = [0]
    stop = [False]

    def one(tid):
        if stop[0]:
            return
        if (time.time() - T0) / 60 >= MAX_MINUTES:
            stop[0] = True
            return
        try:
            got = look_up(tid, tid in shared)
        except SystemExit:
            stop[0] = True
            raise
        except Exception:
            got = None              # (review) one odd answer counts as a failed look-up, not the end of the run
        with lock:
            asked[0] += 1
            if got is None:
                failed[0] += 1
            else:
                films[tid] = [got[0], got[1], TODAY]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for _ in ex.map(one, todo):
            pass
    say('Looked up: %d (%d could not be asked); %s' % (asked[0], failed[0], 'stopped at the time limit' if stop[0] else 'all done'))

    # The file for the app: each name with its films' year, minutes and (shared names only) cast.
    out = {}
    for tid, w in wanted.items():
        f = films.get(tid)
        if not f or f[0] <= 0:
            continue
        p = plain(w[2] if len(w) > 2 else '')
        y = year_of(w[0]) if w else 0
        if not p or y <= 0:
            continue
        entry = [y, int(f[0])]
        if tid in shared and f[1]:
            entry.append(f[1])
        out.setdefault(p, []).append(entry)
    save('film_index.json.gz', {'v': 1, 'made': int(time.time()), 'films': out})
    save('films_state.json.gz', {'films': films})
    n_films = sum(len(v) for v in out.values())
    say('In the file: %d names, %d films' % (len(out), n_films))

    lines = ['Premierium films - run of %s UTC (took %d:%02d)' % (datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'), (time.time() - T0) // 60, (time.time() - T0) % 60), '']
    lines.append('Films in the ratings list: %d; looked up: %d; with a running time: %d; never looked up yet: %d' % (
        len(wanted), len(films), sum(1 for f in films.values() if f[0] > 0), sum(1 for t in wanted if t not in films)))
    lines.append('')
    lines.append('Samples:')
    for s in SAMPLES:
        lines.append('  %s: %s' % (s, json.dumps(out.get(s, 'not in the file yet'), ensure_ascii=False)[:300]))
    lines.append('')
    lines.append('Run log:')
    lines += ['  ' + hide(l) for l in log]
    with open('films_report.txt', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines[:8]))


if __name__ == '__main__':
    try:
        main()
    except (SystemExit, Exception) as e:
        msg = hide('%s: %s' % (type(e).__name__, e)) if not isinstance(e, SystemExit) else hide(e)
        with open('films_report.txt', 'w', encoding='utf-8') as f:
            f.write('Premierium films - run of %s UTC FAILED\n\n%s\n\nRun log:\n%s\n' % (datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'), msg, '\n'.join('  ' + hide(l) for l in log)))
        print('::error::' + msg)
        sys.exit(1)
