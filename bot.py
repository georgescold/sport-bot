"""
bot.py — Bot Discord persistant "Road to sub 38"
Tourne 24h/24 sur Railway. Gère :
  - !seance              → séance du jour avec boutons
  - !programme <date> : <description> → écrit dans le Google Sheet
  - Clic ✅              → ouvre une modale ressentis + km
  - Clic ❌              → marque séance non réalisée
  - Soumission modale    → écrit ressentis + km dans le sheet
"""

import os, re, json, asyncio
from datetime import date
import discord
from discord.ext import commands
from discord import ui
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIG ────────────────────────────────────
DISCORD_BOT_TOKEN    = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID   = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_USER_ID      = int(os.environ["DISCORD_USER_ID"])
SPREADSHEET_ID       = os.environ["SPREADSHEET_ID"]
SHEET_NAME           = "Données Loys"  # NE PAS MODIFIER — jamais écrire dans "Données Nico"
SA_JSON              = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

COL_SEANCE = 2; COL_RESSENTIS = 5; COL_KM_JOUR = 7; COL_DATE = 0; COL_JOUR = 1

JOURS_FR = {"Monday":"Lundi","Tuesday":"Mardi","Wednesday":"Mercredi",
            "Thursday":"Jeudi","Friday":"Vendredi","Saturday":"Samedi","Sunday":"Dimanche"}
MOIS_FR  = {"janvier":1,"février":2,"mars":3,"avril":4,"mai":5,"juin":6,
            "juillet":7,"août":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12}

# ── GOOGLE SHEETS ─────────────────────────────
def sheets():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def get_rows():
    r = sheets().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A:H").execute()
    return r.get("values", [])

def find_row(rows, d):
    for i, r in enumerate(rows):
        if r and r[0].strip() == d: return i
    return None

def write_cell(row, col, val):
    sheets().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{chr(65+col)}{row+1}",
        valueInputOption="RAW", body={"values": [[val]]}).execute()

def parse_date(text):
    text = text.strip().lower(); y = date.today().year
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m: return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?", text)
    if m: return f"{int(m.group(3) or y):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\s+(\w+)", text)
    if m:
        mo = MOIS_FR.get(m.group(2))
        if mo: return f"{y}-{mo:02d}-{int(m.group(1)):02d}"
    return None

# ── VUES DISCORD (boutons + modale) ───────────
class RessentisModal(ui.Modal, title="🏃 Ta séance du jour"):
    ressentis = ui.TextInput(label="Ressentis + FC moyenne à l'effort",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: Bonne séance, jambes légères. FC moy: 145 bpm",
        min_length=2, max_length=500)
    km = ui.TextInput(label="Kilomètres réalisés",
        placeholder="Ex: 5.2", min_length=1, max_length=10)

    def __init__(self, row_index):
        super().__init__()
        self.row_index = row_index

    async def on_submit(self, interaction: discord.Interaction):
        r = str(self.ressentis); k = str(self.km).replace(",", ".")
        await asyncio.to_thread(write_cell, self.row_index, COL_RESSENTIS, r)
        await asyncio.to_thread(write_cell, self.row_index, COL_KM_JOUR, k)
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Super, c'est noté ! 🎉\n"
                     f"**Ressentis :** {r}\n**Km :** {k} km\n"
                     f"Bravo pour la séance ! 🏃‍♂️🔥"),
            view=None)


class SeanceView(ui.View):
    def __init__(self, row_index, seance_txt):
        super().__init__(timeout=None)
        self.row_index  = row_index
        self.seance_txt = seance_txt

    @ui.button(label="✅ Séance faite", style=discord.ButtonStyle.success,
               custom_id="btn_valider")
    async def valider(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RessentisModal(self.row_index))

    @ui.button(label="❌ Non réalisée", style=discord.ButtonStyle.danger,
               custom_id="btn_non_realise")
    async def non_realise(self, interaction: discord.Interaction, button: ui.Button):
        await asyncio.to_thread(write_cell, self.row_index, COL_SEANCE,
                                f"{self.seance_txt} — ❌ Non réalisée")
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Pas de souci, c'est noté ! 💪\n"
                     f"Séance marquée comme non réalisée. À demain !"),
            view=None)


# ── BOT ───────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Connecté : {bot.user}")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching,
                                  name="ton plan d'entraînement 🏃"))

async def send_seance(channel):
    today_str = date.today().isoformat()
    today_day = JOURS_FR[date.today().strftime("%A")]
    date_fr   = date.today().strftime("%d/%m/%Y")
    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, today_str)
    if row_idx is None:
        await channel.send(f"<@{DISCORD_USER_ID}> Aucune séance trouvée pour aujourd'hui ({today_str}) 🤔")
        return
    row        = rows[row_idx]
    seance_txt = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else "Repos"
    jour       = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else today_day
    view = SeanceView(row_idx, seance_txt)
    await channel.send(
        content=(f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
                 f"**📅 Séance — {jour} {date_fr}**\n> **{seance_txt}**\n\n"
                 f"Clique sur un bouton une fois ta séance terminée 👇"),
        view=view)

@bot.command(name="seance")
async def cmd_seance(ctx):
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    try: await ctx.message.delete()
    except: pass
    await send_seance(ctx.channel)

@bot.command(name="programme")
async def cmd_programme(ctx, *, args: str = ""):
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    m = re.match(r"(.+?)\s*:\s*(.+)", args)
    if not m:
        await ctx.reply("Format : `!programme <date> : <séance>`\nEx: `!programme mardi 16 juin : footing 30min`")
        return
    date_raw, seance = m.group(1).strip(), m.group(2).strip()
    date_iso = parse_date(date_raw)
    if not date_iso:
        await ctx.reply(f"Date `{date_raw}` non reconnue. Essaie `16 juin`, `16/06` ou `2026-06-16`.")
        return
    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, date_iso)
    if row_idx is None:
        await ctx.reply(f"Aucune ligne trouvée pour **{date_raw}** ({date_iso}) dans le planning.")
        return
    await asyncio.to_thread(write_cell, row_idx, COL_SEANCE, seance)
    try:
        d = date.fromisoformat(date_iso)
        affiche = f"{JOURS_FR[d.strftime('%A')]} {d.strftime('%d/%m/%Y')}"
    except: affiche = date_iso
    await ctx.reply(f"✅ **{affiche}** → **{seance}**\nC'est noté dans ton plan 📋")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    print(f"Erreur : {error}")

bot.run(DISCORD_BOT_TOKEN)
