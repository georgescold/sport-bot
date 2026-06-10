import os

import sys, json, requests as rq
from google.oauth2 import service_account
import google.auth.transport.requests as g_req_transport

SERVICE_ACCOUNT_FILE = "/home/user/sport-bot/gen-lang-client-0218641615-b114179ddeb2.json"
SPREADSHEET_ID = "1jCijlZVgIGK-8TCbM9zwv7c3F4BSsMZNlTHV_iMzFPM"
SHEET_NAME = "Données Loys"
COMMANDS_SHEET = "Commandes"
DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = "1513887659339288676"
DISCORD_API = "https://discord.com/api/v10"


def _get_token():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    req = g_req_transport.Request()
    creds.refresh(req)
    return creds.token


def _sheets_get(token, range_):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rq.utils.quote(range_, safe='!:')}"
    r = rq.get(url, headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json().get("values", [])


def _sheets_update(token, range_, value):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{rq.utils.quote(range_, safe='!:')}"
    params = {"valueInputOption": "RAW"}
    body = {"values": [[value]]}
    r = rq.put(url, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
               params=params, json=body)
    r.raise_for_status()
    return r.json()


def find_row_for_date(rows, target_date):
    for i, row in enumerate(rows):
        if row and row[0].strip() == target_date:
            return i
    return None


def update_cell(token, row_index, col_index, value):
    col_letter = chr(ord("A") + col_index)
    sheet_row = row_index + 1
    range_ = f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    _sheets_update(token, range_, value)
    print(f"  ✅ Sheet : {range_} = '{value}'")


def get_all_rows(token):
    return _sheets_get(token, f"'{SHEET_NAME}'!A:H")


def mark_command_done(token, sheet_row, reponse):
    _sheets_update(token, f"'{COMMANDS_SHEET}'!C{sheet_row + 1}", "done")
    _sheets_update(token, f"'{COMMANDS_SHEET}'!D{sheet_row + 1}", reponse[:500])


def send_message(content):
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    try:
        r = rq.post(f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
                    headers=headers, json={"content": content}, timeout=10)
        if r.status_code in (200, 201):
            print(f"  ✅ Discord : message envoyé")
            return True
        else:
            print(f"  ⚠️  Discord bloqué ({r.status_code}): {r.text[:100]}")
            return False
    except Exception as e:
        print(f"  ⚠️  Discord inaccessible : {e}")
        return False


if __name__ == "__main__":
    plan = json.load(sys.stdin)
    token = _get_token()
    rows = get_all_rows(token)
    applied = []

    for e in plan.get("edits", []):
        idx = find_row_for_date(rows, e["date"])
        if idx is None:
            applied.append("date " + e["date"] + " introuvable")
            continue
        col = int(e["col"])
        if col in (0, 1, 9):
            applied.append("colonne " + str(col) + " protegee")
            continue
        update_cell(token, idx, col, str(e["value"]))
        applied.append(chr(65 + col) + "@" + e["date"] + "=" + str(e["value"]))

    msg = (plan.get("discord_message") or "").strip()
    discord_sent = False
    if msg:
        discord_sent = send_message(msg)

    mark_command_done(token, int(plan["command_row"]), plan.get("result") or "; ".join(applied))
    print(json.dumps({"applied": applied, "discord_sent": discord_sent}, ensure_ascii=False))
