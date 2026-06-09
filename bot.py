"""
bot.py — Bot Discord persistant "Road to sub 38"
-------------------------------------------------
Tourne 24h/24 sur Railway. Gère :
  !seance              → envoie immédiatement la séance du jour avec boutons
  !programme <date> : <description> → écrit dans le Google Sheet

Les boutons (✅/❌) et modales restent gérés par Vercel (api/discord.py).
"""

import os
import re
import json
import asyncio
from datetime import date

import discord
from discord.ext import commands
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ──────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────
DISCORD_BOT_TOKEN        = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID       = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_USER_ID          = int(os.environ["DISCORD_USER_ID"])
SPREADSHEET_ID           = os.environ["SPREADSHEET_ID"]
SHEET_NAME               = os.environ.get("SHEET_NAME", "Données Loys")
SERVICE_ACCOUNT_JSON     = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

COL_DATE      = 0
COL_JOUR      = 1
COL_SEANCE    = 2
COL_RESSENTIS = 5
COL_KM_JOUR   = 7

JOURS_FR = {
    "Monday": "Lundi", "Tuesday": "Mardi", "Wednesday": "Mercredi",
    "Thursday": "Jeudi", "Friday": "Vendredi", "Saturday": "Samedi", "Sunday": "Dimanche"
}

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
    info = json.loads(SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def get_all_rows():
    svc = get_sheets_service()
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!A:H"
    ).execute()
    return result.get("values", [])


def find_row_for_date(rows, target_date: str):
    for i, row in enumerate(rows):
        if row and len(row) > COL_DATE and row[COL_DATE].strip() == target_date:
            return i
    return None


def update_cell(row_index: int, col_index: int, value: str):
    svc = get_sheets_service()
    col_letter = chr(ord("A") + col_index)
    cell_range = f"'{SHEET_NAME}'!{col_letter}{row_index + 1}"
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=cell_range,
        valueInputOption="RAW",
        body={"values": [[value]]}
    ).execute()


def parse_date_fr(text: str):
    text = text.strip().lower()
    current_year = date.today().year

    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    m = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if m:
        y = int(m.group(3)) if m.group(3) else current_year
        return f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"

    m = re.search(r"(\d{1,2})\s+(\w+)", text)
    if m:
        day = int(m.group(1))
        month = MOIS_FR.get(m.group(2).lower())
        if month:
            return f"{current_year}-{month:02d}-{day:02d}"

    return None


def build_reminder_payload(seance: str, jour: str, date_fr: str, row_index: int) -> dict:
    return {
        "content": (
            f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
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
                        "style": 3,
                        "label": "✅ Séance faite",
                        "custom_id": f"valider_{row_index}"
                    },
                    {
                        "type": 2,
                        "style": 4,
                        "label": "❌ Non réalisée",
                        "custom_id": f"non_realise_{row_index}"
                    }
                ]
            }
        ]
    }


# ──────────────────────────────────────────────
# BOT DISCORD
# ──────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Bot connecté : {bot.user} (ID: {bot.user.id})")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="ton plan d'entraînement 🏃"
        )
    )


@bot.command(name="seance")
async def seance(ctx):
    """!seance → envoie la séance du jour avec boutons."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return

    today_str = date.today().isoformat()
    today_day = JOURS_FR[date.today().strftime("%A")]
    date_fr   = date.today().strftime("%d/%m/%Y")

    try:
        rows    = await asyncio.to_thread(get_all_rows)
        row_idx = find_row_for_date(rows, today_str)
    except Exception as e:
        await ctx.send(f"❌ Erreur Google Sheets : {e}")
        return

    if row_idx is None:
        await ctx.send(
            f"<@{DISCORD_USER_ID}> Aucune séance trouvée pour aujourd'hui "
            f"({today_str}) dans le planning. 🤔"
        )
        return

    row    = rows[row_idx]
    seance_txt = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else "Repos"
    jour   = row[COL_JOUR].strip() if len(row) > COL_JOUR else today_day

    payload = build_reminder_payload(seance_txt, jour, date_fr, row_idx)

    await ctx.message.delete()   # Supprime le message "!seance" pour garder le channel propre
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    await channel.send(
        content=payload["content"],
        components=payload["components"] if hasattr(discord, "ui") else None
    )

    # Envoi via l'API raw pour supporter les components
    import aiohttp
    headers = {
        "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
        "Content-Type": "application/json"
    }
    async with aiohttp.ClientSession() as session:
        await session.post(
            f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages",
            headers=headers,
            json=payload
        )


@bot.command(name="programme")
async def programme(ctx, *, args: str = ""):
    """!programme <date> : <description> → écrit dans le sheet."""
    if ctx.channel.id != DISCORD_CHANNEL_ID:
        return

    m = re.match(r"(.+?)\s*:\s*(.+)", args)
    if not m:
        await ctx.reply(
            "Format incorrect 🙁\n"
            "Utilise : `!programme <date> : <description>`\n"
            "Exemples :\n"
            "• `!programme mardi 16 juin : footing 30min`\n"
            "• `!programme 16/06 : vélo 1h`"
        )
        return

    date_raw = m.group(1).strip()
    seance   = m.group(2).strip()
    date_iso = parse_date_fr(date_raw)

    if not date_iso:
        await ctx.reply(
            f"Je n'ai pas réussi à lire la date `{date_raw}` 🙁\n"
            f"Essaie : `16 juin`, `16/06` ou `2026-06-16`"
        )
        return

    try:
        rows    = await asyncio.to_thread(get_all_rows)
        row_idx = find_row_for_date(rows, date_iso)
    except Exception as e:
        await ctx.reply(f"❌ Erreur Google Sheets : {e}")
        return

    if row_idx is None:
        await ctx.reply(
            f"Aucune ligne trouvée pour **{date_raw}** ({date_iso}) dans le planning.\n"
            f"Vérifie que cette date existe dans le Google Sheet."
        )
        return

    try:
        await asyncio.to_thread(update_cell, row_idx, COL_SEANCE, seance)
    except Exception as e:
        await ctx.reply(f"❌ Erreur lors de l'écriture dans le sheet : {e}")
        return

    try:
        d       = date.fromisoformat(date_iso)
        jour    = JOURS_FR[d.strftime("%A")]
        affiche = f"{jour} {d.strftime('%d/%m/%Y')}"
    except Exception:
        affiche = date_iso

    await ctx.reply(
        f"✅ Séance programmée !\n"
        f"**{affiche}** → **{seance}**\n"
        f"C'est noté dans ton plan 📋"
    )


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Erreur commande : {error}")


# ──────────────────────────────────────────────
# LANCEMENT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
