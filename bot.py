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
    weekly_summary_task.start()
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

# ─────────────────────────────────────────────
# RÉSUMÉ HEBDOMADAIRE — tous les dimanches à 21h
# ─────────────────────────────────────────────
import io
import datetime
from zoneinfo import ZoneInfo
from discord.ext import tasks
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

COL_KM_SEMAINE = 9   # J

PARIS = ZoneInfo("Europe/Paris")

def get_weekly_data():
    """
    Retourne :
      - weeks      : liste de (label_semaine, km_total, nb_seances)
                     sur les 16 dernières semaines
      - this_week  : dict avec stats de la semaine en cours
    """
    rows = get_rows()
    today = date.today()

    # Semaine ISO en cours : lundi → dimanche
    week_start = today - datetime.timedelta(days=today.weekday())  # lundi
    week_end   = week_start + datetime.timedelta(days=6)           # dimanche

    # Remonter 16 semaines en arrière
    weeks = {}
    for row in rows[1:]:   # skip header
        if not row or not row[0].strip():
            continue
        try:
            d = date.fromisoformat(row[0].strip())
        except ValueError:
            continue

        # km journée
        km_val = 0.0
        if len(row) > COL_KM_JOUR and row[COL_KM_JOUR].strip():
            try:
                km_val = float(row[COL_KM_JOUR].strip().replace(",", "."))
            except ValueError:
                pass

        # Séance faite ?
        seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        is_rest = "repos" in seance.lower() or seance == "" or "Non réalisée" in seance

        iso_week = d.isocalendar()[:2]   # (année, semaine)
        if iso_week not in weeks:
            weeks[iso_week] = {"km": 0.0, "seances": 0, "start": d - datetime.timedelta(days=d.weekday())}
        weeks[iso_week]["km"]      += km_val
        if km_val > 0 or (not is_rest and seance):
            weeks[iso_week]["seances"] += 1

    # Garder les 16 dernières semaines
    sorted_weeks = sorted(weeks.items())[-16:]

    # Stats semaine en cours
    current_iso = today.isocalendar()[:2]
    this_week   = weeks.get(current_iso, {"km": 0.0, "seances": 0})

    return sorted_weeks, this_week, week_start, week_end


def generate_chart(sorted_weeks) -> io.BytesIO:
    """Génère un graphique Strava-like et retourne un buffer PNG."""
    labels = []
    kms    = []

    for (yr, wk), data in sorted_weeks:
        s = data["start"]
        labels.append(s.strftime("%-d %b").lower())
        kms.append(data["km"])

    fig, ax = plt.subplots(figsize=(10, 4))
    fig.patch.set_facecolor("#1c1c1e")
    ax.set_facecolor("#1c1c1e")

    x = range(len(labels))
    bars = ax.bar(x, kms, color="#FC4C02", width=0.6, zorder=3)

    # Mettre la dernière barre (semaine en cours) plus claire
    if bars:
        bars[-1].set_color("#FF8C6B")

    # Valeurs au-dessus des barres
    for bar, km in zip(bars, kms):
        if km > 0:
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    f"{km:.0f}", ha="center", va="bottom",
                    color="white", fontsize=8, fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=45, ha="right", color="#aaaaaa", fontsize=8)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:.0f} km"))
    ax.tick_params(colors="#aaaaaa")
    ax.spines[:].set_visible(False)
    ax.yaxis.set_tick_params(labelcolor="#aaaaaa")
    ax.grid(axis="y", color="#333333", linewidth=0.5, zorder=0)
    ax.set_title("📈 Progression km — 16 dernières semaines",
                 color="white", fontsize=12, pad=12)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor="#1c1c1e")
    plt.close(fig)
    buf.seek(0)
    return buf


@tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=PARIS))
async def weekly_summary_task():
    """S'exécute tous les jours à 21h Paris — n'agit que le dimanche."""
    if date.today().weekday() != 6:
        return

    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    sorted_weeks, this_week, week_start, week_end = await asyncio.to_thread(get_weekly_data)

    km    = this_week["km"]
    nb    = this_week["seances"]
    debut = week_start.strftime("%d/%m")
    fin   = week_end.strftime("%d/%m")

    texte = (
        f"📊 **Résumé de la semaine — {debut} au {fin}**\n\n"
        f"🏃 **{km:.1f} km** parcourus cette semaine\n"
        f"✅ **{nb} séance(s)** réalisée(s)\n\n"
        f"<@{DISCORD_USER_ID}> Belle semaine, continue comme ça ! 💪"
    )

    buf = await asyncio.to_thread(generate_chart, sorted_weeks)
    file = discord.File(buf, filename="progression_km.png")
    await channel.send(content=texte, file=file)


@weekly_summary_task.before_loop
async def before_weekly():
    await bot.wait_until_ready()
