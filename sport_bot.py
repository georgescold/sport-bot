"""
sport_bot.py - Outils de la routine cloud
-----------------------------------------
MODE --reminder      → envoie le rappel de séance Discord (DÉPRÉCIÉ : le rappel est
                       désormais planifié à 8h00 par bot.py, tâche morning_reminder_task.
                       Ne plus appeler depuis la routine cloud → sinon double envoi.
                       Conservé pour un envoi manuel ponctuel.)

Les helpers de l'onglet « Commandes » (ensure_commands_sheet, get_pending_commands,
mark_command_done, format_sheet_context) sont utilisés par la routine cloud et
apply_cmd.py. Le temps réel (boutons, !seance, !programme, !claude) est géré par
le bot Railway (bot.py).
"""

import argparse
import os
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
DISCORD_TOKEN        = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID           = "1513887659339288676"
USER_ID              = "340479270449315840"
SERVICE_ACCOUNT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "gen-lang-client-0218641615-b114179ddeb2.json")

# Colonnes (index 0-based) : disposition « Données Loys » :
# A=date B=jour C=séance … F=ressentis
# G="Notes Nico" (colonne du COACH : NE JAMAIS écrire) H=km_journee I=km_semaine
COL_DATE       = 0   # A
COL_JOUR       = 1   # B
COL_SEANCE     = 2   # C
COL_TYPE       = 4   # E : type de séance (déduit de C si vide ; coach prioritaire)
COL_RESSENTIS  = 5   # F
COL_NOTES_NICO = 6   # G : coach, lecture seule
COL_KM_JOUR    = 7   # H : fallback ; la vraie colonne est résolue par km_jour_col()

DISCORD_API = "https://discord.com/api/v10"

JOURS_FR = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi", "Sunday": "Dimanche"
}

# ──────────────────────────────────────────────
# GOOGLE SHEETS
# ──────────────────────────────────────────────
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    # Les sandbox cloud (Claude Code web) interceptent le TLS via un proxy dont
    # le CA n'est connu que du bundle système ; httplib2 ne le lit pas par défaut.
    ca_bundle = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("SSL_CERT_FILE")
    if ca_bundle and os.path.exists(ca_bundle):
        import httplib2
        import google_auth_httplib2
        authed_http = google_auth_httplib2.AuthorizedHttp(
            creds, http=httplib2.Http(ca_certs=ca_bundle))
        return build("sheets", "v4", http=authed_http, cache_discovery=False)
    return build("sheets", "v4", credentials=creds)


def get_all_rows():
    service = get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A:K"
    ).execute()
    return result.get("values", [])


def find_row_for_date(rows, target_date: str) -> int | None:
    """Retourne l'index (0-based) de la ligne correspondant à target_date (YYYY-MM-DD)."""
    for i, row in enumerate(rows):
        if row and row[COL_DATE].strip() == target_date:
            return i
    return None


JOUR_TO_WD = {"lundi":0,"mardi":1,"mercredi":2,"jeudi":3,
              "vendredi":4,"samedi":5,"dimanche":6}

def _row_set(row, col, val):
    while len(row) <= col: row.append("")
    row[col] = val

def seance_type(seance: str) -> str:
    """Déduit le « Type séance » (col E) depuis le libellé de la séance (col C).
    Vocabulaire aligné sur le coach (Footing / Repos) + types évidents."""
    t = (seance or "").strip().lower()
    if not t:
        return ""
    if t.startswith("repos"):
        return "Repos"
    if "renfo" in t or "muscu" in t:
        return "Renfo"
    if "mobilit" in t:
        return "Mobilité"
    return "Footing"   # toute séance de course (footing, gammes, fractionné, côtes…)

def backfill_types(rows, service=None) -> int:
    """Remplit la colonne E (Type séance) VIDE à partir de la séance (col C).
    Ne touche JAMAIS une cellule E déjà remplie (le coach reste prioritaire).
    Modifie `rows` en place ; renvoie le nombre de cellules complétées."""
    filled = 0
    for i, row in enumerate(rows):
        if i == 0 or not row:
            continue
        seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        if not seance:
            continue
        cur = row[COL_TYPE].strip() if len(row) > COL_TYPE else ""
        if cur:
            continue
        typ = seance_type(seance)
        if not typ:
            continue
        _row_set(row, COL_TYPE, typ)
        filled += 1
        if service is not None:
            try: update_cell(service, i, COL_TYPE, typ)
            except Exception as e: print(f"⚠️ backfill type ligne {i+1} : {e}")
    return filled

def backfill_dates(rows, service=None) -> int:
    """Complète les dates (col A) ET les jours (col B) manquants, et les écrit
    dans le Sheet.

    Le coach remplit parfois la séance (col C) du planning à venir mais laisse la
    date (A) et/ou le jour (B) vides ; sans date, find_row_for_date() ne retrouve
    plus la ligne du jour. Reconstruction :
      • jour manquant mais date présente   → jour déduit de la date ;
      • date manquante mais jour présent   → 1ʳᵉ date après la dernière connue
                                             dont le weekday == col B ;
      • date ET jour manquants (séance présente) → jour consécutif suivant.
    Lignes entièrement vides ignorées. Modifie `rows` en place ; renvoie le nb de
    lignes touchées."""
    last_date = None
    filled = 0
    for i, row in enumerate(rows):
        if i == 0 or not row:
            continue
        a = row[COL_DATE].strip()   if len(row) > COL_DATE   else ""
        b = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else ""
        c = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        if a:
            try: last_date = date.fromisoformat(a)
            except ValueError: continue
            if not b:
                jr = JOURS_FR[last_date.strftime("%A")]
                _row_set(row, COL_JOUR, jr); filled += 1
                if service is not None:
                    try: update_cell(service, i, COL_JOUR, jr)
                    except Exception as e: print(f"⚠️ backfill jour ligne {i+1} : {e}")
            continue
        if last_date is None or (not b and not c):
            continue
        wd = JOUR_TO_WD.get(b.lower()) if b else None
        cand = last_date
        if wd is not None:
            for _ in range(7):
                cand += timedelta(days=1)
                if cand.weekday() == wd:
                    break
            else:
                continue
        else:
            cand += timedelta(days=1)
        _row_set(row, COL_DATE, cand.isoformat())
        if service is not None:
            try: update_cell(service, i, COL_DATE, cand.isoformat(), parse=True)
            except Exception as e: print(f"⚠️ backfill date ligne {i+1} : {e}")
        if not b:
            jr = JOURS_FR[cand.strftime("%A")]
            _row_set(row, COL_JOUR, jr)
            if service is not None:
                try: update_cell(service, i, COL_JOUR, jr)
                except Exception as e: print(f"⚠️ backfill jour ligne {i+1} : {e}")
        last_date = cand
        filled   += 1
    return filled


def update_cell(service, row_index: int, col_index: int, value: str, parse: bool = False):
    """parse=True → USER_ENTERED : Sheets interprète la valeur (nombre, date) au
    lieu de la stocker en texte. Requis pour la date (col A) et les km, dont
    dépendent les formules d'agrégat I..L : SUMIFS ignore le texte.
    parse=False pour le texte libre (une valeur commençant par « = » serait
    sinon interprétée comme une formule)."""
    col_letter = chr(ord("A") + col_index)
    sheet_row  = row_index + 1
    cell_range = f"'{SHEET_NAME}'!{col_letter}{sheet_row}"
    body = {"values": [[value]]}
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell_range,
        valueInputOption="USER_ENTERED" if parse else "RAW",
        body=body
    ).execute()
    print(f"  ✅ Sheet : {cell_range} = '{value}'")


def km_jour_col(rows, default: int = COL_KM_JOUR) -> int:
    """Index 0-based de la colonne « km_journee », résolu via l'en-tête (ligne 0).
    On cible la colonne par son NOM plutôt que par un index figé : l'ajout de
    « Notes Nico » en G a décalé les colonnes. Ne matche jamais « km_semaine ».
    Fallback : COL_KM_JOUR (H)."""
    if rows and rows[0]:
        for i, raw in enumerate(rows[0]):
            n = (raw or "").strip().lower()
            if "km" in n and "jour" in n and "semaine" not in n:
                return i
    return default


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


def send_payload(payload: dict, channel_id: str = None) -> dict:
    """Envoie un payload Discord complet (avec composants/boutons).

    channel_id permet de viser un autre salon que celui de Loys, par exemple
    celui d'Enzo, dont le suivi vit dans enzo.py et dans un autre classeur."""
    resp = requests.post(
        f"{DISCORD_API}/channels/{channel_id or CHANNEL_ID}/messages",
        headers=discord_headers(),
        json=payload
    )
    resp.raise_for_status()
    return resp.json()


def build_reminder_payload(seance: str, jour: str, date_fr: str, row_index: int) -> dict:
    """Construit le payload Discord avec boutons interactifs.

    Les custom_id doivent correspondre EXACTEMENT à ceux de la vue persistante
    SeanceView du bot Railway (bot.py) qui gère les clics : `btn_valider` /
    `btn_non_realise`. Le bot retrouve la ligne du jour dynamiquement, donc on
    n'encode pas row_index dans le custom_id (sinon le clic reste sans réponse
    → « Échec de l'interaction »)."""
    return {
        "content": (
            f"🏃 Hey <@{USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
            f"**📅 Séance : {jour} {date_fr}**\n"
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
                        "custom_id": "btn_valider"
                    },
                    {
                        "type": 2,
                        "style": 4,          # rouge
                        "label": "❌ Non réalisée",
                        "custom_id": "btn_non_realise"
                    }
                ]
            }
        ]
    }


# ──────────────────────────────────────────────
# MODE REMINDER (--reminder)
# ──────────────────────────────────────────────
def run_reminder():
    print(f"🏃 MODE REMINDER : {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    rows = get_all_rows()
    svc_bf = get_sheets_service()
    n  = backfill_dates(rows, service=svc_bf)   # complète col A/B manquantes
    nt = backfill_types(rows, service=svc_bf)   # remplit col E (type) si vide
    if n or nt:
        print(f"  🔧 {n} date(s)/jour(s) + {nt} type(s) complété(s)")
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


# ──────────────────────────────────────────────
# ONGLET « COMMANDES » : helpers routine cloud + apply_cmd.py
# ──────────────────────────────────────────────
COMMANDS_SHEET = "Commandes"


def ensure_commands_sheet():
    svc   = get_sheets_service()
    meta  = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if COMMANDS_SHEET in titles:
        return
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": COMMANDS_SHEET}}}]}
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COMMANDS_SHEET}'!A1:C1",
        valueInputOption="RAW",
        body={"values": [["Timestamp", "Demande", "Statut"]]}
    ).execute()


def get_pending_commands():
    svc = get_sheets_service()
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COMMANDS_SHEET}'!A:C"
    ).execute()
    rows = res.get("values", [])
    pending = []
    for i, row in enumerate(rows):
        if len(row) >= 3 and row[2].strip().lower() == "pending":
            ts      = row[0].strip() if len(row) > 0 else ""
            demande = row[1].strip() if len(row) > 1 else ""
            pending.append((i, ts, demande))
    return pending


def mark_command_done(sheet_row: int, reponse: str):
    svc = get_sheets_service()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COMMANDS_SHEET}'!C{sheet_row + 1}",
        valueInputOption="RAW",
        body={"values": [["done"]]}
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COMMANDS_SHEET}'!D{sheet_row + 1}",
        valueInputOption="RAW",
        body={"values": [[reponse[:500]]]}
    ).execute()


def format_sheet_context(rows) -> str:
    lines = []
    kmc = km_jour_col(rows)
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        date_s = row[0].strip() if len(row) > 0 else ""
        jour   = row[1].strip() if len(row) > 1 else ""
        seance = row[2].strip() if len(row) > 2 else ""
        ress   = row[5].strip() if len(row) > 5 else ""
        km     = row[kmc].strip() if len(row) > kmc else ""
        lines.append(
            f"{date_s} | {jour} | Séance: {seance or '-'} | "
            f"Ressentis: {ress or '-'} | Km: {km or '-'}"
        )
    return "\n".join(lines[-60:])


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reminder",     action="store_true")
    args = parser.parse_args()

    if args.reminder:
        run_reminder()
    else:
        print("Usage: sport_bot.py --reminder")
        sys.exit(1)
