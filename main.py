import fire
import os
import json
import re
import time
from datetime import datetime, timedelta
from difflib import SequenceMatcher

import requests
from scholarly import scholarly, ProxyGenerator
from serpapi import GoogleSearch
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from trello import create_card, list_cards, archive_card, update_card, \
    SCIENCE_TOPROCESS_LIST_ID, SCIENCE_UNCLASSIFIED_LIST_ID

load_dotenv()

YEAR_RE = re.compile(r'\b(19|20)\d{2}\b')
# A title match above this means the hit is the searched paper
AUTO_PICK_SCORE = 0.9


def _user_input(text, opts):
    while True:
        val = input('{} [{}]: '.format(text, '/'.join(opts)))
        if val in opts:
            break
    return val


def _similarity(query, title):
    return SequenceMatcher(None, query.lower(), (title or '').lower()).ratio()


def _sort_by_relevance(found_pubs, article_name):
    """Sort results so the closest title match to the query comes first."""
    return sorted(
        found_pubs,
        key=lambda p: _similarity(article_name, p.get('bib', {}).get('title', '')),
        reverse=True)


def _best_score(found_pubs, article_name):
    return max(
        (_similarity(article_name, p.get('bib', {}).get('title', ''))
         for p in found_pubs),
        default=0)


def pick_pub(found_pubs, article_name):
    if not found_pubs:
        print('Found no publication with the name: {}'.format(article_name))
        return None

    found_pubs = _sort_by_relevance(found_pubs, article_name)
    best = found_pubs[0]
    best_score = _similarity(article_name, best.get('bib', {}).get('title', ''))

    if len(found_pubs) == 1:
        return best

    # Auto-pick when the top hit is clearly the searched paper
    if best_score > AUTO_PICK_SCORE:
        print('Auto-picked best match ({:.0%} title match): {}'.format(
            best_score, best['bib'].get('title', '')))
        return best

    print('Found multiple publications:')
    for idx, pub in enumerate(found_pubs):
        bib = pub.get('bib', {})
        print('===== Publication: {} ({:.0%} title match)\n{}'.format(
            idx,
            _similarity(article_name, bib.get('title', '')),
            '\n'.join([
                '\t{}\t{}'.format(h, bib.get(h, '?'))
                for h in ['title', 'author', 'pub_year', 'venue']])))

    opts = [str(opt) for opt in range(len(found_pubs))] + ['q']
    picked = _user_input('Pick the desired publication (q to skip)', opts)
    if picked == 'q':
        return None
    return found_pubs[int(picked)]


def _get_authors(pub):
    author = pub.get('bib', {}).get('author', '')
    if isinstance(author, str):
        return author.replace(' and ', '; ')[:50]
    if isinstance(author, list):
        names = [a if isinstance(a, str) else a.get('name', '') for a in author]
        if len(names) > 4:
            return ', '.join(names[:3]) + ',.. , ' + names[-1]
        return ', '.join(names)
    return str(author)


def _get_card_name(pub):
    bib = pub.get('bib', {})
    return '{} ({} {}) - {}'.format(
        bib.get('title', '?'),
        bib.get('pub_year', '?'),
        bib.get('venue', '?'),
        _get_authors(pub))


def _get_card_desc(pub):
    return '{}\n\n{}'.format(
        pub.get('pub_url', ''),
        pub.get('bib', {}).get('abstract', ''))


def _parse_summary(summary, structured_authors=None):
    """Parse a Google Scholar byline into (authors, venue, pub_year).

    Handles both formats seen in the wild:
      old: "S Cole, B Voytek - Journal of neurophysiology, 2019 - journals.physiology.org"
      new: "S Cole, B VoytekJournal of neurophysiology, 2019•journals.physiology.org"
    """
    summary = summary or ''
    authors, venue, pub_year = '', '', 0

    m = YEAR_RE.search(summary)
    if m:
        pub_year = int(m.group(0))

    if '•' in summary:
        # New glued format: authors and venue are concatenated before the year
        body = summary.split('•')[0]
    else:
        # Old format. Split on ' - ' (with surrounding spaces) so hyphenated
        # author names and venues do not get chopped up.
        parts = [p.strip() for p in summary.split(' - ')]
        if len(parts) >= 2:
            authors = parts[0]
            body = parts[1]
        else:
            body = parts[0]

    # The venue is whatever precedes the year in the body
    if pub_year and str(pub_year) in body:
        venue = body.split(str(pub_year))[0].rstrip(' ,')
    else:
        venue = body.strip().rstrip(' ,')

    # Prefer the structured author list when the API provides one; it is the
    # only reliable way to separate authors from the venue in the new format.
    if structured_authors:
        names = [a.get('name', '') for a in structured_authors if a.get('name')]
        if names:
            authors = ', '.join(names)
            for name in names:
                venue = venue.replace(name, '')
            venue = venue.strip(' ,')

    return authors, venue, pub_year


def _process_html_result(res):
    title_el = res.find(class_='gs_rt')
    if not title_el or not title_el.a:
        return None
    title = title_el.a.text
    pub_url = title_el.a.attrs.get('href', '')

    ggs = res.find(class_='gs_ggs')
    if ggs and ggs.a:
        pub_url = ggs.a.attrs.get('href', pub_url)

    author, venue, pub_year = [], '', 0
    metadata = res.find(class_='gs_a')
    if metadata:
        author = [aut.text for aut in metadata.find_all('a')
                  if 'sra' in aut.attrs.get('href', '')]
        parsed_authors, venue, pub_year = _parse_summary(metadata.text)
        if not author:
            author = parsed_authors

    abstract_el = res.find(class_='gs_rs')
    abstract = abstract_el.text if abstract_el else ''

    return {
        'bib': {
            'title': title,
            'pub_year': pub_year,
            'author': author,
            'venue': venue,
            'abstract': abstract,
        },
        'pub_url': pub_url
    }


def _stringify(pub):
    headers = ['container_type', 'bib', 'filled', 'gsrank', 'pub_url',
               'author_id', 'num_citations', 'url_scholarbib', 'url_add_sclib',
               'citedby_url', 'url_related_articles']
    return dict([(h, pub[h]) for h in headers if h in pub])


def pick_and_send(found_pubs, article_name):
    """Returns True if a card was created."""
    print('Picking from {}...'.format(len(found_pubs)))
    pub = pick_pub(found_pubs, article_name)
    if pub is None:
        print('Nothing to send.')
        return False

    details = {
        'name': _get_card_name(pub),
        'desc': _get_card_desc(pub),
        'pos': 'top'
    }
    print('Creating card...')
    create_card(SCIENCE_UNCLASSIFIED_LIST_ID, details)
    print('done!')
    return True


def _serp_call(article_name, use_hl=True, no_cache=True):
    """Single SerpAPI call. Returns the raw organic_results list (may be empty).

    hl is optional and no_cache defaults on. SerpAPI's Google scrape has been
    intermittently returning empty or irrelevant results (a known upstream
    issue since early 2026); hl was an early suspect and no_cache avoids being
    handed a previously-cached bad response.
    """
    params = {
        'engine': 'google_scholar',
        'q': article_name,
        'num': 10,
        'api_key': os.environ['SERP_API_KEY'],
    }
    if use_hl:
        params['hl'] = 'en'
    if no_cache:
        params['no_cache'] = True

    results = GoogleSearch(params).get_dict()
    if 'error' in results:
        print('  serpapi error: {}'.format(results['error']))
        return []
    return results.get('organic_results', []) or []


def _query_serp_and_parse(article_name, max_retries=3):
    # SerpAPI is currently flaky (~50% success on some queries). Retry a few
    # times, and on the last attempt drop hl in case it is degrading results.
    organic = []
    for attempt in range(max_retries):
        use_hl = attempt < max_retries - 1
        organic = _serp_call(article_name, use_hl=use_hl)
        best = max(
            (_similarity(article_name, r.get('title', '')) for r in organic),
            default=0)
        if organic and best > 0.5:
            break
        if attempt < max_retries - 1:
            label = 'empty' if not organic else 'low relevance ({:.0%})'.format(best)
            print('  serp attempt {} {}; retrying{}...'.format(
                attempt + 1, label, '' if use_hl else ' without hl'))
            time.sleep(2)

    pubs = []
    for res in organic:
        pub_info = res.get('publication_info', {})
        try:
            authors, venue, pub_year = _parse_summary(
                pub_info.get('summary', ''),
                structured_authors=pub_info.get('authors'))
        except Exception as err:
            print('Warning: could not parse summary "{}" ({})'.format(
                pub_info.get('summary', ''), err))
            authors, venue, pub_year = '', '', 0

        pub_url = res.get('link', '')
        if not pub_url:
            resources = res.get('resources') or []
            if resources and resources[0].get('link'):
                pub_url = resources[0]['link']

        pubs.append({
            'bib': {
                'title': res.get('title', ''),
                'pub_year': pub_year,
                'author': authors,
                'venue': venue,
                'abstract': res.get('snippet', ''),
            },
            'pub_url': pub_url
        })
    return pubs


# ---------------------------------------------------------------------------
# Alternative backends: real academic metadata APIs (no Google scraping).
# Each returns the same {'bib': {...}, 'pub_url': ...} shape as the serp path,
# so they plug straight into pick_and_send.
# ---------------------------------------------------------------------------

OPENALEX_URL = 'https://api.openalex.org/works'
SEMANTIC_SCHOLAR_URL = 'https://api.semanticscholar.org/graph/v1/paper/search'


def _reconstruct_abstract(inverted_index):
    """OpenAlex stores abstracts as {word: [positions]}. Rebuild the text."""
    if not inverted_index:
        return ''
    positions = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return ' '.join(word for _, word in positions)


def _openalex_search(article_name):
    """Query OpenAlex. Free API key required since 2026-02-13.

    Get one at https://openalex.org/settings/api and set OPENALEX_API_KEY.
    """
    params = {
        'search': article_name,
        'per_page': 10,
    }
    api_key = os.environ.get('OPENALEX_API_KEY')
    if api_key:
        params['api_key'] = api_key
    else:
        print('  note: no OPENALEX_API_KEY set; sharing the anonymous '
              '$0.10/day (~100 searches) budget of this IP. Get a free key '
              'with $1/day at openalex.org/settings/api.')
    # Politeness identifier; harmless if the key is present.
    mailto = os.environ.get('OPENALEX_MAILTO')
    if mailto:
        params['mailto'] = mailto

    pubs = []
    try:
        resp = requests.get(OPENALEX_URL, params=params, timeout=30)
        resp.raise_for_status()
        results = resp.json().get('results', [])
    except Exception as err:
        print('  openalex error: {}'.format(err))
        return pubs

    for work in results:
        # venue lives under primary_location.source; every level can be null
        loc = work.get('primary_location') or {}
        source = loc.get('source') or {}
        venue = source.get('display_name', '')

        authors = [
            (a.get('author') or {}).get('display_name', '')
            for a in work.get('authorships', [])
        ]
        authors = [a for a in authors if a]

        doi = work.get('doi')  # already a full https://doi.org/... URL
        pub_url = doi or loc.get('landing_page_url') or work.get('id', '')

        pubs.append({
            'bib': {
                'title': work.get('display_name') or work.get('title', ''),
                'pub_year': work.get('publication_year', 0),
                'author': authors,
                'venue': venue,
                'abstract': _reconstruct_abstract(
                    work.get('abstract_inverted_index')),
            },
            'pub_url': pub_url
        })
    return pubs


def _semanticscholar_search(article_name):
    """Query Semantic Scholar. Works with no key (rate-limited; may 429).

    Set SEMANTIC_SCHOLAR_API_KEY for higher limits if you have one.
    """
    params = {
        'query': article_name,
        'limit': 10,
        'fields': 'title,year,authors,venue,abstract,url,externalIds',
    }
    headers = {}
    api_key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY')
    if api_key:
        headers['x-api-key'] = api_key

    pubs = []
    try:
        resp = requests.get(SEMANTIC_SCHOLAR_URL, params=params,
                            headers=headers, timeout=30)
        if resp.status_code == 429:
            print('  semanticscholar: rate limited (429). Wait a bit, or set '
                  'SEMANTIC_SCHOLAR_API_KEY.')
            return pubs
        resp.raise_for_status()
        results = resp.json().get('data', []) or []
    except Exception as err:
        print('  semanticscholar error: {}'.format(err))
        return pubs

    for paper in results:
        authors = [a.get('name', '') for a in paper.get('authors', [])]
        authors = [a for a in authors if a]

        pub_url = paper.get('url', '')
        if not pub_url:
            doi = (paper.get('externalIds') or {}).get('DOI')
            if doi:
                pub_url = 'https://doi.org/{}'.format(doi)

        pubs.append({
            'bib': {
                'title': paper.get('title', ''),
                'pub_year': paper.get('year') or 0,
                'author': authors,
                'venue': paper.get('venue', ''),
                'abstract': paper.get('abstract') or '',
            },
            'pub_url': pub_url
        })
    return pubs


# Map --backend flag values to their search function.
ALT_BACKENDS = {
    'openalex': _openalex_search,
    'semanticscholar': _semanticscholar_search,
}


def _search_google_scholar(
        article_name,
        run_proxy=False,
        force_download=False,
        force_serp=False,
        results_dir='results/'):
    """Scrape Google Scholar with scholarly, falling back to SerpAPI."""
    if force_serp:
        return _query_serp_and_parse(article_name)

    if run_proxy:
        pg = ProxyGenerator()
        pg.ScraperAPI(os.environ['SCRAPPER_API'])
        scholarly.use_proxy(pg)

    scholarly.set_retries(1)

    os.makedirs(results_dir, exist_ok=True)

    print('Searching...')
    found_pubs = []
    query_fname = os.path.join(
        results_dir, 'query_{}.json'.format(article_name.replace('/', '_')))
    if os.path.isfile(query_fname) and not force_download:
        print('  ..loading all from file')
        with open(query_fname, 'r') as f:
            found_pubs = json.load(f)
    else:
        try:
            for idx, pub in enumerate(scholarly.search_pubs(article_name)):
                print('  ..adding')
                found_pubs.append(pub)
                time.sleep(1)
                if idx == 5:
                    # only 1 call. Should be in top 5
                    break
        except Exception as err:
            print('Failed with: {}. Try:'.format(err))
            print('https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={}&btnG='.format(
                article_name.replace(' ', '+')))
        if not found_pubs:
            print('Failed to find article with name... trying serp now')
            found_pubs = _query_serp_and_parse(article_name)
        # Only cache successful queries, so a failed run does not poison
        # future attempts with an empty result file
        if found_pubs:
            with open(query_fname, 'w') as fout:
                fout.write(json.dumps([_stringify(pub) for pub in found_pubs]))

    return found_pubs


def _search_auto(article_name, **gs_kwargs):
    """Try each source in turn until one returns the searched paper.

    Google Scholar (scholarly and SerpAPI alike) now often answers exact-title
    queries with unrelated papers, so the metadata APIs go first and Google
    Scholar is the last resort. If no source clears AUTO_PICK_SCORE, the
    closest results are returned for a manual pick.
    """
    sources = [
        ('openalex', lambda: _openalex_search(article_name)),
        ('semanticscholar', lambda: _semanticscholar_search(article_name)),
        ('google scholar',
         lambda: _search_google_scholar(article_name, **gs_kwargs)),
    ]
    best_pubs, best_score = [], 0
    for name, search in sources:
        print('Searching via {}...'.format(name))
        pubs = search()
        score = _best_score(pubs, article_name)
        if score > best_score:
            best_pubs, best_score = pubs, score
        if score > AUTO_PICK_SCORE:
            break
        print('  best title match only {:.0%}'.format(score))
    return best_pubs


def search_gs(
        article_name,
        backend='auto',
        run_proxy=False,
        force_download=False,
        force_serp=False,
        results_dir='results/'):
    """Find article_name and send it to the Unclassified list.

    backend: 'auto' (OpenAlex, then Semantic Scholar, then Google Scholar),
    'openalex', 'semanticscholar' or 'serpapi' (Google Scholar only).
    Returns True if a card was created.
    """
    gs_kwargs = dict(run_proxy=run_proxy, force_download=force_download,
                     force_serp=force_serp, results_dir=results_dir)
    if backend == 'auto':
        found_pubs = _search_auto(article_name, **gs_kwargs)
    elif backend in ALT_BACKENDS:
        print('Searching via {}...'.format(backend))
        found_pubs = ALT_BACKENDS[backend](article_name)
    else:
        found_pubs = _search_google_scholar(article_name, **gs_kwargs)
    return pick_and_send(found_pubs, article_name)


def search_next_trello(
        fname='gs_log.txt',
        backend='auto',
        force=False,
        force_download=False,
        run_proxy=False,
        force_serp=False):
    last_time = datetime.fromtimestamp(0)
    if os.path.isfile(fname):
        with open(fname) as f:
            last_time = datetime.fromtimestamp(float(f.read()))
    now = datetime.now()

    if now - last_time < timedelta(hours=2):
        print('Need more time!')
        if force:
            print('.. forcing')
        else:
            print('.. exiting')
            return

    cards = list_cards(SCIENCE_TOPROCESS_LIST_ID)
    if len(cards) > 0:
        card = cards[0]
        print('Processing {}'.format(card['name']))
        sent = search_gs(card['name'], backend=backend, run_proxy=run_proxy,
                         force_download=force_download, force_serp=force_serp)
        with open(fname, 'w') as out:
            out.write(str(datetime.now().timestamp()))
        if sent:
            archive_card(card['id'])
        else:
            # Keep the card rather than losing the paper, but move it out of
            # the way so the next run does not get stuck retrying it
            print('Moving card to the bottom of the list to retry later')
            update_card(card['id'], {'pos': 'bottom'})
    else:
        print('Nothing to process')


def read_html(html_filename):
    html_doc = None
    with open(html_filename) as f:
        html_doc = f.read()

    soup = BeautifulSoup(html_doc, 'html.parser')

    gs_results_section = soup.find_all('div', id='gs_res_ccl_mid')
    assert len(gs_results_section) == 1, \
        'Found {} mid sections'.format(len(gs_results_section))
    gs_results_section = gs_results_section[0]
    results = gs_results_section.find_all(class_='gs_scl')
    found_pubs = [_process_html_result(res) for res in results]
    found_pubs = [p for p in found_pubs if p]
    query_name = html_filename.split('/')[-1].split('- Google Scholar')[0]
    pick_and_send(found_pubs, query_name)


if __name__ == '__main__':
    fire.Fire({
        'search': search_gs,
        'next-trello': search_next_trello,
        'list-cards': list_cards,
        'read-html': read_html,
    })
