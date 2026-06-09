"""
bot.py — Bot Discord "Road to sub 38"
  !seance                  → séance du jour avec boutons
  !programme <date>:<desc> → modifie le planning
  !claude <demande>        → modification IA du sheet (langage naturel)
  Bouton ✅                → modale ressentis + km
  Bouton ❌                → séance non réalisée
  21h05 quotidien          → rappel si séance non renseignée (ou msg neutre si vide)
  21h00 dimanche           → résumé hebdomadaire + graphique Strava-like
"""

import os, re, json, asyncio, io, datetime
from datetime import date
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import ui
from google.oauth2 import service_account
from googleapiclient.discovery import build

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── CONFIG ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
DISCORD_USER_ID    = int(os.environ["DISCORD_USER_ID"])
SPREADSHEET_ID     = os.environ["SPREADSHEET_ID"]
SHEET_NAME         = "Données Loys"  # NE JAMAIS écrire dans "Données Nico"
SA_JSON            = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

PARIS = ZoneInfo("Europe/Paris")

COL_DATE      = 0
COL_JOUR      = 1
COL_SEANCE    = 2
COL_RESSENTIS = 5
COL_KM_JOUR   = 7

JOURS_FR = {
    "Monday":"Lundi","Tuesday":"Mardi","Wednesday":"Mercredi",
    "Thursday":"Jeudi","Friday":"Vendredi","Saturday":"Samedi","Sunday":"Dimanche"
}
MOIS_FR = {
    "janvier":1,"février":2,"mars":3,"avril":4,"mai":5,"juin":6,
    "juillet":7,"août":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12
}
MOIS_ABBR = {
    1:"JAN",2:"FÉV",3:"MAR",4:"AVR",5:"MAI",6:"JUIN",
    7:"JUI",8:"AOÛ",9:"SEP",10:"OCT",11:"NOV",12:"DÉC"
}

# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
def sheets_svc():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds)

def get_rows():
    r = sheets_svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A:J").execute()
    return r.get("values", [])

def find_row(rows, d):
    for i, r in enumerate(rows):
        if r and r[0].strip() == d: return i
    return None

def write_cell(row, col, val):
    sheets_svc().spreadsheets().values().update(
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

def format_sheet_context(rows) -> str:
    lines = []
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        date_s = row[0].strip() if len(row) > 0 else ""
        jour   = row[1].strip() if len(row) > 1 else ""
        seance = row[2].strip() if len(row) > 2 else ""
        ress   = row[5].strip() if len(row) > 5 else ""
        km     = row[7].strip() if len(row) > 7 else ""
        lines.append(f"{date_s} | {jour} | Séance: {seance or '—'} | Ressentis: {ress or '—'} | Km: {km or '—'}")
    return "\n".join(lines[-60:])

# ── GRAPHIQUE STRAVA-LIKE ─────────────────────────────────────────────────────
def get_weekly_data():
    rows  = get_rows()
    today = date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    week_end   = week_start + datetime.timedelta(days=6)
    weeks = {}
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        try: d = date.fromisoformat(row[0].strip())
        except: continue
        km_val = 0.0
        if len(row) > COL_KM_JOUR and row[COL_KM_JOUR].strip():
            try: km_val = float(row[COL_KM_JOUR].strip().replace(",","."))
            except: pass
        seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        iso_week = d.isocalendar()[:2]
        if iso_week not in weeks:
            weeks[iso_week] = {"km":0.0,"seances":0,"start":d-datetime.timedelta(days=d.weekday())}
        weeks[iso_week]["km"] += km_val
        is_rest = "repos" in seance.lower() or seance=="" or "Non réalisée" in seance
        if km_val > 0 or (not is_rest and seance):
            weeks[iso_week]["seances"] += 1
    sorted_weeks = sorted(weeks.items())[-16:]
    this_week    = weeks.get(today.isocalendar()[:2], {"km":0.0,"seances":0})
    return sorted_weeks, this_week, week_start, week_end

def generate_chart(sorted_weeks) -> io.BytesIO:
    labels = [data["start"] for _, data in sorted_weeks]
    kms    = [data["km"]    for _, data in sorted_weeks]
    x      = list(range(len(kms)))
    x_ticks, x_labels, last_month = [], [], None
    for i, d in enumerate(labels):
        if d.month != last_month:
            x_ticks.append(i); x_labels.append(MOIS_ABBR[d.month]); last_month = d.month
    BG = "#191919"; ORANGE = "#FC4C02"; GRID = "#2a2a2a"
    fig, ax = plt.subplots(figsize=(11, 4.5))
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    ax.fill_between(x, kms, color=ORANGE, alpha=0.25, zorder=1)
    ax.bar(x, kms, color=ORANGE, alpha=0.18, width=0.8, zorder=1)
    ax.plot(x, kms, color=ORANGE, linewidth=2.2, zorder=3)
    ax.scatter(x, kms, color=ORANGE, s=38, zorder=4, edgecolors=BG, linewidths=1.5)
    if kms:
        peak_i = kms.index(max(kms))
        if kms[peak_i] > 0:
            ax.annotate(f"{kms[peak_i]:.0f} km", xy=(peak_i, kms[peak_i]),
                        xytext=(0, 10), textcoords="offset points",
                        ha="center", color="white", fontsize=9, fontweight="bold")
    ax.set_xticks(x_ticks); ax.set_xticklabels(x_labels, color="#888888", fontsize=10, fontweight="bold")
    ax.tick_params(axis="x", length=0, pad=8)
    max_km = max(kms) if kms else 10
    ax.set_ylim(0, max_km * 1.25)
    yticks = [0, round(max_km / 2), round(max_km)]
    ax.set_yticks(yticks); ax.set_yticklabels([f"{v} km" for v in yticks], color="#888888", fontsize=9)
    ax.tick_params(axis="y", length=0)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8, zorder=0); ax.xaxis.grid(False)
    for spine in ax.spines.values(): spine.set_visible(False)
    ax.set_xlim(-0.5, len(x) - 0.5)
    plt.tight_layout(pad=1.5)
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, facecolor=BG)
    plt.close(fig); buf.seek(0)
    return buf

# ── VUES DISCORD ──────────────────────────────────────────────────────────────
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
                     f"**Ressentis :** {r}\n**Km :** {k} km\nBravo pour la séance ! 🏃‍♂️🔥"),
            view=None)


class SeanceView(ui.View):
    def __init__(self, row_index, seance_txt):
        super().__init__(timeout=None)
        self.row_index  = row_index
        self.seance_txt = seance_txt

    @ui.button(label="✅ Séance faite", style=discord.ButtonStyle.success, custom_id="btn_valider")
    async def valider(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(RessentisModal(self.row_index))

    @ui.button(label="❌ Non réalisée", style=discord.ButtonStyle.danger, custom_id="btn_non_realise")
    async def non_realise(self, interaction: discord.Interaction, button: ui.Button):
        await asyncio.to_thread(write_cell, self.row_index, COL_SEANCE,
                                f"{self.seance_txt} — ❌ Non réalisée")
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Pas de souci, c'est noté ! 💪\n"
                     f"Séance marquée comme non réalisée. À demain !"),
            view=None)

# ── BOT ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


async def send_seance_msg(channel):
    """Envoie le message de séance du jour avec boutons."""
    today_str = date.today().isoformat()
    today_day = JOURS_FR[date.today().strftime("%A")]
    date_fr   = date.today().strftime("%d/%m/%Y")
    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, today_str)
    if row_idx is None:
        await channel.send(f"<@{DISCORD_USER_ID}> Aucune séance trouvée pour aujourd'hui ({today_str}) 🤔")
        return
    row        = rows[row_idx]
    seance_txt = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
    jour       = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else today_day
    if not seance_txt:
        await channel.send(f"📅 Aucune séance programmée aujourd'hui ({date_fr}).")
        return
    view = SeanceView(row_idx, seance_txt)
    await channel.send(
        content=(f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
                 f"**📅 Séance — {jour} {date_fr}**\n> **{seance_txt}**\n\n"
                 f"Clique sur un bouton une fois ta séance terminée 👇"),
        view=view)


@bot.event
async def on_ready():
    print(f"✅ Connecté : {bot.user}")
    weekly_summary_task.start()
    evening_check_task.start()
    check_new_courses_task.start()
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching,
                                  name="ton plan d'entraînement 🏃"))


@bot.command(name="seance")
async def cmd_seance(ctx):
    """!seance → envoie la séance du jour avec boutons."""
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    try: await ctx.message.delete()
    except: pass
    await send_seance_msg(ctx.channel)


@bot.command(name="programme")
async def cmd_programme(ctx, *, args: str = ""):
    """!programme <date> : <séance> → modifie une ligne du planning."""
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


COMMANDS_SHEET = "Commandes"

@bot.command(name="claude")
async def cmd_claude(ctx, *, request: str = ""):
    """!claude <demande> → enregistre dans l'onglet Commandes pour traitement Cowork."""
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    if not request:
        await ctx.reply(
            "Dis-moi ce que tu veux modifier ! 💬\n"
            "Ex: `!claude j'ai oublié mes km du lundi 3 juin, c'était 8.5 km`\n"
            "Ex: `!claude mon ressenti du 5 juin : FC moy 148, jambes lourdes`\n"
            "Ex: `!claude la séance du 10 juin était un footing 45min`"
        )
        return
    try:
        ts  = datetime.datetime.now(PARIS).strftime("%Y-%m-%d %H:%M")
        svc = sheets_svc()
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{COMMANDS_SHEET}'!A:C",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [[ts, request, "pending"]]}
        ).execute()
        await ctx.reply("✍️ Demande reçue ! Je la traite dans quelques minutes... ⏳")
    except Exception as e:
        await ctx.reply(f"❌ Erreur : {e}")
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    print(f"Erreur commande : {error}")


# ── TÂCHE 1 : RAPPEL 21h05 (tous les jours) ──────────────────────────────────
@tasks.loop(time=datetime.time(hour=21, minute=5, tzinfo=PARIS))
async def evening_check_task():
    """Rappel si séance non renseignée — message neutre si aucune séance programmée."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return

    today_str = date.today().isoformat()
    date_fr   = date.today().strftime("%d/%m/%Y")
    today_day = JOURS_FR[date.today().strftime("%A")]

    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, today_str)

    # Pas de ligne pour aujourd'hui
    if row_idx is None:
        await channel.send(f"📅 Aucune séance programmée aujourd'hui ({date_fr}).")
        return

    row        = rows[row_idx]
    seance_txt = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""

    # Case vide ou repos → message neutre, pas de tag
    if not seance_txt or "repos" in seance_txt.lower():
        await channel.send(f"😴 Pas de séance programmée aujourd'hui — bonne récupération ! 🛋️")
        return

    # Déjà répondu → rien faire
    km_val    = row[COL_KM_JOUR].strip()   if len(row) > COL_KM_JOUR   else ""
    ressentis = row[COL_RESSENTIS].strip() if len(row) > COL_RESSENTIS else ""
    if km_val or ressentis or "Non réalisée" in seance_txt:
        return

    # Séance prévue mais pas encore renseignée → retag avec boutons
    view = SeanceView(row_idx, seance_txt)
    await channel.send(
        content=(f"⏰ <@{DISCORD_USER_ID}> Tu n'as pas encore renseigné ta séance ! 👀\n\n"
                 f"**📅 Rappel — {today_day} {date_fr}**\n> **{seance_txt}**\n\n"
                 f"Valide ta séance avant de dormir 👇"),
        view=view)


@evening_check_task.before_loop
async def before_evening():
    await bot.wait_until_ready()


# ── TÂCHE 2 : RÉSUMÉ HEBDO (dimanche 21h) ────────────────────────────────────
@tasks.loop(time=datetime.time(hour=21, minute=0, tzinfo=PARIS))
async def weekly_summary_task():
    """Dimanche à 21h → résumé semaine + graphique progression."""
    if date.today().weekday() != 6: return
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return

    sorted_weeks, this_week, week_start, week_end = await asyncio.to_thread(get_weekly_data)
    km    = this_week["km"]
    nb    = this_week["seances"]
    debut = week_start.strftime("%d/%m")
    fin   = week_end.strftime("%d/%m")

    texte = (f"📊 **Résumé de la semaine — {debut} au {fin}**\n\n"
             f"🏃 **{km:.1f} km** parcourus cette semaine\n"
             f"✅ **{nb} séance(s)** réalisée(s)\n\n"
             f"<@{DISCORD_USER_ID}> Belle semaine, continue comme ça ! 💪")

    buf  = await asyncio.to_thread(generate_chart, sorted_weeks)
    file = discord.File(buf, filename="progression_km.png")
    await channel.send(content=texte, file=file)


@weekly_summary_task.before_loop
async def before_weekly():
    await bot.wait_until_ready()



# ── COURSES ───────────────────────────────────────────────────────────────────
COURSES_SHEET = "Courses"


def get_course_rows():
    r = sheets_svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COURSES_SHEET}'!A:E"
    ).execute()
    return r.get("values", [])


def update_course_status(row_idx: int, status: str):
    """Met à jour la colonne Statut (E) d'une course — row_idx 0-based."""
    sheets_svc().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COURSES_SHEET}'!E{row_idx + 1}",
        valueInputOption="RAW",
        body={"values": [[status]]}
    ).execute()


class CourseView(ui.View):
    def __init__(self, row_index: int, nom: str):
        super().__init__(timeout=None)
        self.row_index = row_index
        self.nom       = nom
        btn_oui = ui.Button(
            label="👍 Oui, ça m'intéresse !",
            style=discord.ButtonStyle.success,
            custom_id=f"course_oui_{row_index}"
        )
        btn_non = ui.Button(
            label="👎 Non merci",
            style=discord.ButtonStyle.secondary,
            custom_id=f"course_non_{row_index}"
        )
        btn_oui.callback = self._oui
        btn_non.callback = self._non
        self.add_item(btn_oui)
        self.add_item(btn_non)

    async def _oui(self, interaction: discord.Interaction):
        await asyncio.to_thread(update_course_status, self.row_index, "oui")
        await interaction.response.edit_message(
            content=f"✅ **{self.nom}**\nNotée comme intéressante ! 🏅 Pense à t'inscrire.",
            view=None
        )

    async def _non(self, interaction: discord.Interaction):
        await asyncio.to_thread(update_course_status, self.row_index, "non")
        await interaction.response.edit_message(
            content=f"❌ **{self.nom}**\nPas pour toi, c'est noté. 👍",
            view=None
        )


# ── TÂCHE 3 : NOTIFICATIONS NOUVELLES COURSES ────────────────────────────────
@tasks.loop(minutes=20)
async def check_new_courses_task():
    """Vérifie l'onglet Courses et notifie pour chaque course avec statut 'nouveau'."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return
    try:
        rows = await asyncio.to_thread(get_course_rows)
    except Exception:
        return

    for i, row in enumerate(rows):
        if not row or len(row) < 5:
            continue
        if row[4].strip().lower() != "nouveau":
            continue

        date_txt = row[0].strip() if len(row) > 0 else "Date à confirmer"
        lieux    = row[1].strip() if len(row) > 1 else ""
        distance = row[2].strip() if len(row) > 2 else ""
        info     = row[3].strip() if len(row) > 3 else ""

        # Séparer nom et lien (format : "Nom de la course | https://...")
        nom, lien = info, ""
        if " | " in info:
            parts = info.split(" | ", 1)
            nom, lien = parts[0].strip(), parts[1].strip()

        lien_txt = f"\n🔗 {lien}" if lien else ""
        content  = (
            f"🏃 <@{DISCORD_USER_ID}> Nouvelle course à proximité !\n\n"
            f"**{nom}**\n"
            f"📅 {date_txt}\n"
            f"📍 {lieux}\n"
            f"🎽 {distance}{lien_txt}\n\n"
            f"Tu t'inscris ? 👇"
        )

        # Marquer notifié AVANT d'envoyer pour éviter les doublons si le bot redémarre
        await asyncio.to_thread(update_course_status, i, "notifié")

        view = CourseView(i, nom)
        await channel.send(content=content, view=view)
        await asyncio.sleep(3)  # pause entre les messages


@check_new_courses_task.before_loop
async def before_courses():
    await bot.wait_until_ready()


# ── LANCEMENT ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
