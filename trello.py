import requests
import os
import json

TRELLO_CARDS_URL = "https://api.trello.com/1/cards"
TRELLO_LIST_CARDS = "https://api.trello.com/1/lists/{}/cards"
TRELLO_CARD_URL = "https://api.trello.com/1/cards/{}"
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
