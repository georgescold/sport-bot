"""
sport_bot.py — Script unifié
-----------------------------
Gère trois cas :

MODE --reminder   (appelé par la tâche planifiée 17h45)
  → Lit la séance du jour dans le sheet, envoie le rappel Discord

MODE --check      (appelé toutes les 30 min, 7h-22h)
  → Détecte les commandes Discord et les réponses au rappel quotidien

COMMANDES DISCORD reconnues par --check :
  !seance              → envoie immédiatement la séance du jour (même flux que 17h45)
  !programme <date> : <description>
                       → écrit la séance dans le sheet pour la date indiquée
  Réponses au rappel   → détecte ressentis + km ou ❌ et met à jour le sheet
"""

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────
SPREADSHEET_ID       = "1jCijlZVgIGK-8TCbM9zwv7c3F4BSsMZNlTHV_iMzFPM"
SHEET_NAME           = "Données Loys"
DISCORD_TOKEN        = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID           = "1513887659339288676"
USER_ID              = "340479270449315840"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gen-lang-client-0218641615-b114179ddeb2.json")
STATE_FILE           = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sport_state.json")
CMD_STATE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sport_cmd_state.json")

# Colonnes (index 0-based)
COL_DATE      = 0   # A
COL_JOUR      = 1   # B
COL_SEANCE    = 2   # C
COL_RESSENTIS = 5   # F
COL_KM_JOUR   = 7   # H

DISCORD_API = "https://discord.com/api/v10"

JOURS_FR = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi", "Sunday": "Dimanche"
}

# Mapping jour FR → numéro weekday (lundi=0)
JOURS_TO_WEEKDAY = {
    "lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3,
    "vendredi": 4, "samedi": 5, "dimanche": 6
}

MOIS_FR = {
    "janvier": 1, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12
}


# ──────────────────────────────────────────────
# GOOGLE SHEETS
# ──────────────────────────────────────────────
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def get_all_rows():
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A:H"
    ).execute()
    return result.get("values", [])


def find_row_for_date(rows, target_date: str) -> int | None:
    """Retourne l'index (0-based) de la ligne correspondant à target_date (YYYY-MM-DD)."""
    for i, row in enumerate(rows):
        if row and row[COL_DATE].strip() == target_date:
            return i
    return None


def update_cell(service, row_index: int, col_index: int, value: str):
    col_letter = chr(ord("A") + col_index)
    sheet_row  = row_index + 1
    cell_range = f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    body = {"values": [[value]]}
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell_range,
        valueInputOption="RAW",
        body=body
    ).execute()
    print(f"  ✅ Sheet : {cell_range} = '{value}'")


# ──────────────────────────────────────────────
# DISCORD
# ──────────────────────────────────────────────
def discord_headers():
    return {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}


def send_message(content: str) -> dict:
    resp = requests.post(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        headers=discord_headers(),
        json={"content": content}
    )
    resp.raise_for_status()
    return resp.json()


def send_payload(payload: dict) -> dict:
    """Envoie un payload Discord complet (avec composants/boutons)."""
    resp = requests.post(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        headers=discord_headers(),
        json=payload
    )
    resp.raise_for_status()
    return resp.json()


def get_messages_after(after_id: str, limit: int = 50) -> list:
    resp = requests.get(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        headers=discord_headers(),
        params={"after": after_id, "limit": limit}
    )
    resp.raise_for_status()
    return list(reversed(resp.json()))


def get_latest_message_id() -> str | None:
    """Retourne l'ID du dernier message du channel."""
    resp = requests.get(
        f"{DISCORD_API}/channels/{CHANNEL_ID}/messages",
        headers=discord_headers(),
        params={"limit": 1}
    )
    resp.raise_for_status()
    msgs = resp.json()
    return msgs[0]["id"] if msgs else None


# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
def load_json(path: str) -> dict:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def extract_km(text: str) -> str:
    text = text.replace(",", ".").strip()
    m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:km|kilomètres?|kms?)?\b", text, re.IGNORECASE)
    return m.group(1) if m else text


def parse_date_fr(text: str) -> str | None:
    """
    Essaie de parser une date en français vers YYYY-MM-DD.
    Formats supportés :
      - "lundi 16 juin"  /  "16 juin"
      - "16/06"  /  "16/06/2026"
      - "2026-06-16"
    """
    text = text.strip().lower()
    current_year = date.today().year

    # Format ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Format JJ/MM ou JJ/MM/AAAA
    m = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if m:
        y = int(m.group(3)) if m.group(3) else current_year
        return f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    # Format "lundi 16 juin" ou "16 juin"
    m = re.search(r"(\d{1,2})\s+(\w+)", text)
    if m:
        day  = int(m.group(1))
        mois = m.group(2).lower()
        month = MOIS_FR.get(mois)
        if month:
            return f"{current_year}-{month:02d}-{day:02d}"

    return None


def build_reminder_payload(seance: str, jour: str, date_fr: str, row_index: int) -> dict:
    """Construit le payload Discord avec boutons interactifs."""
    return {
        "content": (
            f"🏃 Hey <@{USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
            f"**📅 Séance — {jour} {date_fr}**\n"
            f"> **{seance}**\n\n"
            f"Clique sur un bouton une fois ta séance terminée 👇"
        ),
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,          # vert
                        "label": "✅ Séance faite",
                        "custom_id": f"valider_{row_index}"
                    },
                    {
                        "type": 2,
                        "style": 4,          # rouge
                        "label": "❌ Non réalisée",
                        "custom_id": f"non_realise_{row_index}"
                    }
                ]
            }
        ]
    }


# ──────────────────────────────────────────────
# MODE REMINDER (--reminder)
# ──────────────────────────────────────────────
def run_reminder():
    print(f"🏃 MODE REMINDER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    rows = get_all_rows()
    today_str = date.today().isoformat()
    today_day = JOURS_FR[date.today().strftime("%A")]
    row_idx   = find_row_for_date(rows, today_str)

    if row_idx is None:
        print(f"❌ Aucune ligne trouvée pour {today_str} dans le sheet.")
        sys.exit(1)

    row    = rows[row_idx]
    seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else "Repos"
    jour   = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else today_day
    date_fr = date.today().strftime("%d/%m/%Y")

    # Vérification cohérence
    if jour and jour != today_day:
        print(f"⚠️  Incohérence jour : sheet='{jour}', attendu='{today_day}'")

    payload = build_reminder_payload(seance, jour, date_fr, row_idx)
    sent = send_payload(payload)
    print(f"✅ Message Discord envoyé (ID: {sent['id']})")

    state = {
        "message_id": sent["id"],
        "row_index": row_idx,
        "seance": seance,
        "sent_at": datetime.now().isoformat(),
        "responded": False
    }
    save_json(STATE_FILE, state)
    print(f"✅ État sauvegardé → {STATE_FILE}")


# ──────────────────────────────────────────────
# MODE CHECK (--check)
# ──────────────────────────────────────────────
def handle_seance_command():
    """!seance → envoie immédiatement la séance du jour et démarre le flux."""
    print("  → Commande !seance détectée")
    rows      = get_all_rows()
    today_str = date.today().isoformat()
    today_day = JOURS_FR[date.today().strftime("%A")]
    row_idx   = find_row_for_date(rows, today_str)

    if row_idx is None:
        send_message(f"<@{USER_ID}> Aucune séance trouvée pour aujourd'hui ({today_str}) dans le planning. 🤔")
        return

    row    = rows[row_idx]
    seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else "Repos"
    jour   = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else today_day
    date_fr = date.today().strftime("%d/%m/%Y")

    payload = build_reminder_payload(seance, jour, date_fr, row_idx)
    sent = send_payload(payload)

    state = {
        "message_id": sent["id"],
        "row_index": row_idx,
        "seance": seance,
        "sent_at": datetime.now().isoformat(),
        "responded": False
    }
    save_json(STATE_FILE, state)
    print(f"  ✅ Rappel envoyé (ID: {sent['id']}) + état sauvegardé")


def handle_programme_command(text: str):
    """!programme <date> : <description> → écrit dans le sheet."""
    print(f"  → Commande !programme : '{text}'")

    # Parser "!programme <date> : <description>"
    m = re.match(r"!programme\s+(.+?)\s*:\s*(.+)", text, re.IGNORECASE)
    if not m:
        send_message(
            f"<@{USER_ID}> Format incorrect 🙁\n"
            f"Utilise : `!programme <date> : <description de la séance>`\n"
            f"Exemples :\n"
            f"• `!programme mardi 16 juin : footing 30min`\n"
            f"• `!programme 16/06 : vélo 1h`\n"
            f"• `!programme 2026-06-16 : natation`"
        )
        return

    date_raw  = m.group(1).strip()
    seance    = m.group(2).strip()
    date_iso  = parse_date_fr(date_raw)

    if not date_iso:
        send_message(
            f"<@{USER_ID}> Je n'ai pas réussi à lire la date `{date_raw}` 🙁\n"
            f"Essaie le format : `16 juin`, `16/06` ou `2026-06-16`"
        )
        return

    rows    = get_all_rows()
    row_idx = find_row_for_date(rows, date_iso)

    if row_idx is None:
        send_message(
            f"<@{USER_ID}> Aucune ligne trouvée pour le **{date_raw}** ({date_iso}) dans le planning.\n"
            f"Vérifie que cette date existe dans le Google Sheet."
        )
        return

    service = get_sheets_service()
    update_cell(service, row_idx, COL_SEANCE, seance)

    # Formatter la date pour l'affichage
    try:
        d     = date.fromisoformat(date_iso)
        jour  = JOURS_FR[d.strftime("%A")]
        affiche = f"{jour} {d.strftime('%d/%m/%Y')}"
    except Exception:
        affiche = date_iso

    send_message(
        f"<@{USER_ID}> ✅ Séance programmée !\n"
        f"**{affiche}** → **{seance}**\n"
        f"C'est noté dans ton plan d'entraînement 📋"
    )
    print(f"  ✅ Séance '{seance}' écrite pour {date_iso} (ligne {row_idx + 1})")


def process_daily_responses(state: dict):
    """Gère les réponses au rappel quotidien (ressentis, km, ❌)."""
    message_id = state["message_id"]
    row_index  = state["row_index"]

    messages  = get_messages_after(message_id)
    user_msgs = [m for m in messages if m.get("author", {}).get("id") == USER_ID]

    if not user_msgs:
        print("  ⏳ Aucune réponse de l'utilisateur.")
        return

    print(f"  📨 {len(user_msgs)} message(s) utilisateur.")

    # Détecter ❌
    for msg in user_msgs:
        content = msg.get("content", "").strip()
        if "❌" in content or content.lower() in ("non", "pas fait", "x"):
            print("  ❌ Séance non faite.")
            svc = get_sheets_service()
            update_cell(svc, row_index, COL_SEANCE, f"{state['seance']} — ❌ Non réalisée")
            send_message(
                f"<@{USER_ID}> Pas de souci, c'est noté ! "
                f"Séance marquée comme non réalisée dans ton plan. 💪 À demain !"
            )
            state["responded"] = True
            save_json(STATE_FILE, state)
            return

    # Cas où on avait déjà les ressentis → ce message est les km
    if state.get("partial_ressentis") and len(user_msgs) >= 1:
        ressentis = state["partial_ressentis"]
        km        = extract_km(user_msgs[0].get("content", "").strip())
        _write_results(state, row_index, ressentis, km)
        return

    # Deux messages → 1er ressentis, 2e km
    if len(user_msgs) >= 2:
        ressentis = user_msgs[0].get("content", "").strip()
        km        = extract_km(user_msgs[1].get("content", "").strip())
        _write_results(state, row_index, ressentis, km)
        return

    # Un seul message → sauvegarder ressentis, attendre km
    if len(user_msgs) == 1:
        ressentis = user_msgs[0].get("content", "").strip()
        print(f"  💾 Ressentis sauvegardés, en attente des km...")
        state["partial_ressentis"] = ressentis
        save_json(STATE_FILE, state)


def _write_results(state: dict, row_index: int, ressentis: str, km: str):
    svc = get_sheets_service()
    update_cell(svc, row_index, COL_RESSENTIS, ressentis)
    update_cell(svc, row_index, COL_KM_JOUR,   km)
    send_message(
        f"<@{USER_ID}> Super, c'est noté ! 🎉\n"
        f"**Ressentis :** {ressentis}\n"
        f"**Km :** {km} km\n"
        f"Bravo pour la séance ! 🏃‍♂️"
    )
    state["responded"] = True
    save_json(STATE_FILE, state)
    print(f"  ✅ Résultats écrits dans le sheet.")


def run_check():
    print(f"🔍 MODE CHECK — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # Charger l'état des commandes (dernier message traité)
    cmd_state    = load_json(CMD_STATE_FILE)
    last_cmd_id  = cmd_state.get("last_processed_id")

    # Si jamais vu de messages, partir du message le plus récent pour éviter le flood
    if not last_cmd_id:
        last_cmd_id = get_latest_message_id()
        save_json(CMD_STATE_FILE, {"last_processed_id": last_cmd_id})
        print("  ℹ️  Initialisation : dernier message ID sauvegardé.")
        # On traite quand même les réponses au rappel si état présent
    else:
        # 1. Récupérer les nouveaux messages depuis la dernière vérification
        new_messages = get_messages_after(last_cmd_id)

        if new_messages:
            print(f"  📬 {len(new_messages)} nouveau(x) message(s) à analyser.")
            new_last_id = new_messages[-1]["id"]

            for msg in new_messages:
                content    = msg.get("content", "").strip()
                author_id  = msg.get("author", {}).get("id", "")

                # Ignorer les messages du bot lui-même
                if msg.get("author", {}).get("bot"):
                    continue

                # Commande !seance
                if content.lower().startswith("!seance"):
                    handle_seance_command()
                    # Mettre à jour le dernier ID traité après envoi
                    new_last_id = msg["id"]
                    break

                # Commande !programme
                if content.lower().startswith("!programme"):
                    handle_programme_command(content)

            save_json(CMD_STATE_FILE, {"last_processed_id": new_last_id})
        else:
            print("  ✉️  Aucun nouveau message.")

    # Les réponses aux boutons (ressentis, km, ❌) sont désormais
    # gérées directement par le serveur Vercel (api/discord.py).
    print("  ℹ️  Réponses aux boutons gérées par Ver