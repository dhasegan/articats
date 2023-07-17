import fire
import os
import json
import time
from datetime import datetime, timedelta

from scholarly import scholarly, ProxyGenerator
from dotenv import load_dotenv
from bs4 import BeautifulSoup

from trello import create_card, list_cards, archive_card, \
    SCIENCE_TOPROCESS_LIST_ID, SCIENCE_UNCLASSIFIED_LIST_ID

load_dotenv()


def _user_input(text, opts):
  while True:
    val = input(text + '[{}]'.format('/'.join(opts)))
    if val in opts:
      break
  return val


def pick_pub(found_pubs, article_name):
  if len(found_pubs) == 1:
    return found_pubs[0]
  elif len(found_pubs) > 1:
    print('Found multiple publications:')
    for idx, pub in enumerate(found_pubs):
      print('===== Publication: {}\n{}'.format(
          idx,
          '\n'.join([
            '\t{}\t{}'.format(h, pub['bib'][h])
            for h in ['title', 'author', 'pub_year', 'venue']])))

    opts = [str(opt) for opt in list(range(len(found_pubs)))]
    picked_idx = int(_user_input('Pick the desired publication', opts))
    return found_pubs[picked_idx]
  else:
    print('Found no publication with the name: {}'.format(article_name))
    return None


def _get_authors(pub):
  author = pub['bib']['author']
  if type(author) == str:
    return author.replace(' and ', '; ')[:50]
  elif type(author) == list:
    if len(author) > 4:
      return ', '.join(author[:3]) + ',.. , ' + author[-1]
    else:
      return ', '.join(author)

  return author


def _get_card_name(pub):
  return '{} ({} {}) - {}'.format(
      pub['bib']['title'],
      pub['bib']['pub_year'],
      pub['bib']['venue'],
      _get_authors(pub))


def _process_html_result(res):
  title = res.find(class_='gs_rt').a
  if not title:
    return None
  title = title.text
  metadata = res.find(class_='gs_a')
  author = [aut.text for aut in metadata.find_all('a') if 'sra' in aut.attrs['href']]
  toks = metadata.text.split('-')[-2].split(',')
  venue = ','.join(toks[:-1]).strip()
  try:
    pub_year = int(toks[-1])
  except Exception as err:
    print('Cannot parse pub_year "{}"'.format(toks[-1]))
    pub_year = 0
  abstract = res.find(class_='gs_rs').text
  pub_url = res.find(class_='gs_rt').a.attrs['href']
  if res.find(class_='gs_ggs'):
    pub_url = res.find(class_='gs_ggs').a.attrs['href']
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

def _get_card_desc(pub):
  return '{}\n\n{}'.format(pub['pub_url'], pub['bib']['abstract'])


def _stringify(pub):
  headers = ['container_type', 'bib', 'filled', 'gsrank', 'pub_url',
             'author_id', 'num_citations', 'url_scholarbib', 'url_add_sclib',
             'citedby_url',   'url_related_articles']
  return dict([(h, pub[h]) for h in headers if h in pub])


def pick_and_send(found_pubs, article_name):
  print('Picking from {}...'.format(len(found_pubs)))
  pub = pick_pub(found_pubs, article_name)

  # print(pub)
  details = {
      'name': _get_card_name(pub),
      'desc': _get_card_desc(pub),
      'pos': 'top'
  }
  print('Creating card...')
  create_card(SCIENCE_UNCLASSIFIED_LIST_ID, details)
  print('done!')


def search_gs(article_name, run_proxy=False, results_dir='results/'):
  if run_proxy:
    pg = ProxyGenerator()
    pg.ScraperAPI(os.environ['SCRAPPER_API'])
    scholarly.use_proxy(pg)

  scholarly.set_retries(1)

  os.makedirs(results_dir, exist_ok=True)

  print('Searching...')
  found_pubs = []
  query_fname = os.path.join(results_dir, 'query_{}.json'.format(article_name.replace('/', '_')))
  if os.path.isfile(query_fname):
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
      print("Failed with: {}. Try:".format(err))
      print("https://scholar.google.com/scholar?hl=en&as_sdt=0%2C5&q={}&btnG=".format(
        article_name.replace(' ', '+')))
    if not found_pubs:
      raise Exception('Failed to find article with name')
    with open(query_fname, 'w') as fout:
      fout.write(json.dumps([_stringify(pub) for pub in found_pubs]))

  pick_and_send(found_pubs, article_name)

def search_next_trello(fname='gs_log.txt', force=False, run_proxy=False):
  last_time = None
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
    search_gs(card['name'], run_proxy=run_proxy)
    with open(fname, 'w') as out:
      out.write(str(datetime.now().timestamp()))
    archive_card(card['id'])
  else:
    print('Nothing to process')


def read_html(html_filename):
  html_doc = None
  with open(html_filename) as f:
    html_doc = f.read()

  soup = BeautifulSoup(html_doc, 'html.parser')

  gs_results_section = soup.find_all('div', id='gs_res_ccl_mid')
  assert len(gs_results_section) == 1, 'Found {} mid sections'.format(len(gs_results_section))
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
