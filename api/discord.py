"""
api/discord.py — Vercel Serverless Function
Gère les interactions Discord (boutons + modales) pour le bot sport "Road to sub 38".

Flux :
  1. Bot envoie un message avec [✅ Séance faite] [❌ Non réalisée]
  2. Clic ✅  → retourne une modale avec champs Ressentis + Km
  3. Submit    → écrit dans Google Sheets + confirme sur Discord
  4. Clic ❌  → marque séance non réalisée dans le sheet + confirme
"""

from http.server import BaseHTTPRequestHandler
import json
import os
import base64
import tempfile

from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ──────────────────────────────────────────────
# CONFIG (variables d'environnement Vercel)
# ──────────────────────────────────────────────
DISCORD_PUBLIC_KEY   = os.environ["DISCORD_PUBLIC_KEY"]
SPREADSHEET_ID       = os.environ["SPREADSHEET_ID"]
SHEET_NAME           = os.environ.get("SHEET_NAME", "Données Loys")
USER_ID              = os.environ["DISCORD_USER_ID"]

# Le JSON du service account est stocké en variable d'env (string JSON)
SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

COL_SEANCE    = 2   # C
COL_RESSENTIS = 5   # F
COL_KM_JOUR   = 7   # H


# ──────────────────────────────────────────────
# GOOGLE SHEETS
# ──────────────────────────────────────────────
def get_sheets_service():
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def update_cell(row_index: int, col_index: int, value: str):
    col_letter = chr(ord("A") + col_index)
    sheet_row  = row_index + 1
    cell_range = f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    svc = get_sheets_service()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell_range,
        valueInputOption="RAW",
        body={"values": [[value]]}
    ).execute()


def get_seance_for_row(row_index: int) -> str:
    svc = get_sheets_service()
    col_letter = chr(ord("A") + COL_SEANCE)
    sheet_row  = row_index + 1
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    ).execute()
    vals = result.get("values", [])
    return vals[0][0] if vals and vals[0] else "séance"


# ──────────────────────────────────────────────
# DISCORD SIGNATURE VERIFICATION
# ──────────────────────────────────────────────
def verify_discord_signature(signature: str, timestamp: str, body: bytes) -> bool:
    try:
        vk = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        vk.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except BadSignatureError:
        return False


# ──────────────────────────────────────────────
# DISCORD INTERACTION RESPONSES
# ──────────────────────────────────────────────
def pong():
    return {"type": 1}


def modal_response(row_index: int, seance: str) -> dict:
    """Retourne une modale avec champs Ressentis + Km."""
    return {
        "type": 9,
        "data": {
            "custom_id": f"ressentis_modal_{row_index}",
            "title": "🏃 Séance terminée !",
            "components": [
                {
                    "type": 1,
                    "components": [{
                        "type": 4,
                        "custom_id": "ressentis",
                        "label": "Ressentis + FC moyenne à l'effort",
                        "style": 2,
                        "placeholder": "Ex: Bonne séance, jambes légères. FC moy: 145 bpm",
                        "min_length": 2,
                        "max_length": 500,
                        "required": True
                    }]
                },
                {
                    "type": 1,
                    "components": [{
                        "type": 4,
                        "custom_id": "km",
                        "label": "Kilomètres réalisés",
                        "style": 1,
                        "placeholder": "Ex: 5.2",
                        "min_length": 1,
                        "max_length": 10,
                        "required": True
                    }]
                }
            ]
        }
    }


def message_update(content: str) -> dict:
    """Met à jour le message original (enlève les boutons après interaction)."""
    return {
        "type": 7,
        "data": {
            "content": content,
            "components": []    # supprime les boutons
        }
    }


def channel_message(content: str) -> dict:
    """Envoie un nouveau message dans le channel."""
    return {
        "type": 4,
        "data": {
            "content": content,
            "components": []
        }
    }


# ──────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────
def handle_button(data: dict) -> dict:
    custom_id = data["data"]["custom_id"]

    # Extraire le row_index depuis le custom_id (ex: "valider_42" ou "non_realise_42")
    parts     = custom_id.rsplit("_", 1)
    action    = parts[0]
    row_index = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None

    if row_index is None:
        return channel_message("❌ Erreur : impossible de retrouver la séance. Contacte le coach bot.")

    # ✅ Bouton "valider" → ouvre la modale
    if action == "valider":
        seance = get_seance_for_row(row_index)
        return modal_response(row_index, seance)

    # ❌ Bouton "non_realise"
    if action == "non_realise":
        seance = get_seance_for_row(row_index)
        update_cell(row_index, COL_SEANCE, f"{seance} — ❌ Non réalisée")
        return message_update(
            f"<@{USER_ID}> Pas de souci, c'est noté dans ton plan ! 💪\n"
            f"**Séance du jour** → ❌ Non réalisée\n"
            f"Repose-toi bien, à demain !"
        )

    return channel_message("❓ Action inconnue.")


def handle_modal(data: dict) -> dict:
    custom_id = data["data"]["custom_id"]

    # Extraire le row_index depuis "ressentis_modal_42"
    parts     = custom_id.rsplit("_", 1)
    row_index = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None

    if row_index is None:
        return channel_message("❌ Erreur : impossible de retrouver la séance.")

    # Extraire les valeurs des champs de la modale
    ressentis = ""
    km        = ""
    for row in data["data"].get("components", []):
        for comp in row.get("components", []):
            if comp["custom_id"] == "ressentis":
                ressentis = comp["value"].strip()
            elif comp["custom_id"] == "km":
                km = comp["value"].strip().replace(",", ".")

    # Écrire dans Google Sheets
    update_cell(row_index, COL_RESSENTIS, ressentis)
    update_cell(row_index, COL_KM_JOUR,   km)

    return channel_message(
        f"<@{USER_ID}> Super, c'est noté ! 🎉\n"
        f"**Ressentis :** {ressentis}\n"
        f"**Km :** {km} km\n"
        f"Bravo pour la séance ! 🏃‍♂️🔥"
    )


# ──────────────────────────────────────────────
# VERCEL HANDLER
# ──────────────────────────────────────────────
class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        # Lire le body
        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # Vérification signature Discord
        signature = self.headers.get("X-Signature-Ed25519", "")
        timestamp  = self.headers.get("X-Signature-Timestamp", "")

        if not verify_discord_signature(signature, timestamp, raw_body):
            self._respond(401, {"error": "Invalid signature"})
            return

        data = json.loads(raw_body)
        interaction_type = data.get("type")

        # Type 1 : PING (vérification de l'endpoint par Discord)
        if interaction_type == 1:
            self._respond(200, pong())
            return

        # Type 3 : MESSAGE_COMPONENT (bouton cliqué)
        if interaction_type == 3:
            response = handle_button(data)
            self._respond(200, response)
            return

        # Type 5 : MODAL_SUBMIT (modale soumise)
        if interaction_type == 5:
            response = handle_modal(data)
            self._respond(200, response)
            return

        self._respond(400, {"error": "Unknown interaction type"})

    def do_GET(self):
        self._respond(200, {"status": "Sport bot is alive 🏃"})

    def _respond(self, status: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass  # Silence les logs HTTP de BaseHTTPRequestHandler
