import requests
import os
import json

TRELLO_CARDS_URL = "https://api.trello.com/1/cards"
TRELLO_LIST_CARDS = "https://api.trello.com/1/lists/{}/cards"
TRELLO_BOARD_CARDS = "https://api.trello.com/1/boards/{}/cards"
TRELLO_CARD_URL = "https://api.trello.com/1/cards/{}"
TRELLO_MY_BOARDS = "https://api.trello.com/1/members/me/boards"
TRELLO_BOARD_LISTS = "https://api.trello.com/1/boards/{}/lists"
SCIENCE_UNCLASSIFIED_LIST_ID="60bfac22fe5ed340bd3313a1"
SCIENCE_TOPROCESS_LIST_ID="60c29a44027c005dd4d6c797"

# https://developer.atlassian.com/cloud/trello/rest/api-group-cards/#api-cards-post

def _q():
  return {
   'key': os.environ['TRELLO_APIKEY'],
   'token': os.environ['TRELLO_TOKEN'],
  }

def create_card(idList, details={}):
  query = {
    **_q(),
    'idList': idList,
    **details
  }

  response = requests.request(
     "POST",
     TRELLO_CARDS_URL,
     params=query
  )
  assert response.text, response
  res_j = json.loads(response.text)
  assert res_j['id'], res_j
  return res_j

def list_cards(idList):
  response = requests.request(
     "GET",
     TRELLO_LIST_CARDS.format(idList),
     params=_q()
  )
  assert response.text, response
  res_j = json.loads(response.text)
  return [card for card in res_j if card['closed'] == False]


def update_card(card_id, details={}):
  query = {
    **_q(),
    **details
  }

  response = requests.request(
     "PUT",
     TRELLO_CARD_URL.format(card_id),
     headers={ "Accept": "application/json" },
     params=query
  )

  assert response.text, response
  res_j = json.loads(response.text)
  assert res_j['id'], res_j
  return res_j

def archive_card(card_id):
  return update_card(card_id, { 'closed': 'true' })


def get_boards(name=None):
  response = requests.request(
     "GET",
     TRELLO_MY_BOARDS,
     params={ **_q(), 'fields': 'name,url,closed' }
  )
  response.raise_for_status()
  boards = [b for b in response.json() if not b['closed']]
  if name:
    boards = [b for b in boards if name.lower() in b['name'].lower()]
  return boards

def get_lists(board_id):
  response = requests.request(
     "GET",
     TRELLO_BOARD_LISTS.format(board_id),
     params={ **_q(), 'filter': 'open', 'fields': 'name' }
  )
  response.raise_for_status()
  return response.json()

def show_boards(name=None):
  """Print board ids of your boards, optionally filtered by a name substring."""
  for b in get_boards(name):
    print(f"{b['id']}\t{b['name']}\t{b['url']}")

def show_lists(board_id):
  """Print the LIST_IDs of a board's open lists."""
  for l in get_lists(board_id):
    print(f"{l['id']}\t{l['name']}")

def find_list(board_name, list_name=None):
  """Print board ids matching board_name and their lists, optionally filtered by list_name."""
  for board in get_boards(board_name):
    print(f"== {board['name']}  BOARD_ID={board['id']}")
    for l in get_lists(board['id']):
      if list_name and list_name.lower() not in l['name'].lower():
        continue
      print(f"   LIST_ID={l['id']}\t{l['name']}")

def export_cards(list_id, output='cards.txt'):
  """Download the open cards of a list and write them into a txt file."""
  cards = list_cards(list_id)
  with open(output, 'w') as f:
    for card in cards:
      f.write(f"{card['name']}\n")
      f.write(f"{card['shortUrl']}\n")
      if card.get('desc'):
        f.write(f"{card['desc']}\n")
      f.write("\n")
  print(f"Wrote {len(cards)} cards to {output}")


if __name__ == '__main__':
  import fire
  from dotenv import load_dotenv
  load_dotenv()
  fire.Fire({
    'boards': show_boards,
    'lists': show_lists,
    'find': find_list,
    'export': export_cards,
  })
