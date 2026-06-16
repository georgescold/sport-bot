"""
bot.py — Bot Discord "Road to sub 38"
  !seance                  → séance du jour avec boutons (instantané, cache du jour)
  !seance semaine          → récap de toutes les séances de la semaine (lun→dim)
  !programme <date> : <s>  → modifie le planning
  !claude <demande>        → modification IA du sheet (langage naturel)
  Bouton ✅                → modale ressentis + km
  Bouton ❌                → séance non réalisée
  21h05 quotidien          → rappel si séance non renseignée (ou msg neutre si vide)
  21h00 dimanche           → résumé hebdomadaire + graphique Strava-like
  toutes les 30 min        → notif si nouvelle note du coach (colonne Notes Nico)
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

# Disposition « Données Loys » : A=date B=jour C=séance … F=ressentis
# G="Notes Nico" (colonne du COACH — NE JAMAIS écrire) H=km_journee I=km_semaine
COL_DATE       = 0   # A
COL_JOUR       = 1   # B
COL_SEANCE     = 2   # C
COL_RESSENTIS  = 5   # F
COL_NOTES_NICO = 6   # G — coach, lecture seule
COL_KM_JOUR    = 7   # H — fallback ; la vraie colonne est résolue par km_jour_col()

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
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET_NAME}'!A:K").execute()
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

def km_jour_col(rows, default: int = COL_KM_JOUR) -> int:
    """Index 0-based de la colonne « km_journee », résolu via l'en-tête (ligne 0).
    On cible la colonne par son NOM plutôt que par un index figé : l'ajout de
    « Notes Nico » en G a décalé les colonnes, et un index codé en dur recasse à
    chaque insertion. Ne matche jamais « km_semaine ». Fallback : COL_KM_JOUR (H)."""
    if rows and rows[0]:
        for i, raw in enumerate(rows[0]):
            n = (raw or "").strip().lower()
            if "km" in n and "jour" in n and "semaine" not in n:
                return i
    return default

def notes_nico_col(rows, default: int = COL_NOTES_NICO) -> int:
    """Index 0-based de la colonne « Notes Nico » (coach), résolu via l'en-tête.
    Robuste aux décalages de colonnes. Fallback : COL_NOTES_NICO (G)."""
    if rows and rows[0]:
        for i, raw in enumerate(rows[0]):
            n = (raw or "").strip().lower()
            if "notes" in n and "nico" in n:
                return i
    return default

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
    kmc = km_jour_col(rows)
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        date_s = row[0].strip() if len(row) > 0 else ""
        jour   = row[1].strip() if len(row) > 1 else ""
        seance = row[2].strip() if len(row) > 2 else ""
        ress   = row[5].strip() if len(row) > 5 else ""
        km     = row[kmc].strip() if len(row) > kmc else ""
        lines.append(f"{date_s} | {jour} | Séance: {seance or '—'} | Ressentis: {ress or '—'} | Km: {km or '—'}")
    return "\n".join(lines[-60:])

# ── GRAPHIQUE STRAVA-LIKE ─────────────────────────────────────────────────────
WEEKS_WINDOW = 13   # ~3 mois affichés (1 trimestre), 1 point = 1 semaine (vue type Strava)

def get_weekly_data():
    rows  = get_rows()
    kmc   = km_jour_col(rows)
    today = date.today()
    week_start = today - datetime.timedelta(days=today.weekday())   # lundi de la semaine en cours
    week_end   = week_start + datetime.timedelta(days=6)

    # Agrège km + séances par semaine (clé = lundi) en IGNORANT les semaines futures :
    # le planning contient des lignes à venir (km=0) qui sinon tirent la courbe à droite.
    km_by_week, seances_by_week = {}, {}
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        try: d = date.fromisoformat(row[0].strip())
        except: continue
        wk = d - datetime.timedelta(days=d.weekday())   # lundi de la ligne
        if wk > week_start: continue                    # semaine future → exclue
        km_val = 0.0
        if len(row) > kmc and row[kmc].strip():
            try: km_val = float(row[kmc].strip().replace(",","."))
            except: pass
        seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        km_by_week[wk] = km_by_week.get(wk, 0.0) + km_val
        is_rest = "repos" in seance.lower() or seance == "" or "Non réalisée" in seance
        if km_val > 0 or (not is_rest and seance):
            seances_by_week[wk] = seances_by_week.get(wk, 0) + 1

    data_weeks = sorted(km_by_week)     # semaines présentes dans le sheet (toutes ≤ semaine en cours)
    this_week  = {"km": km_by_week.get(week_start, 0.0),
                  "seances": seances_by_week.get(week_start, 0)}
    if not data_weeks:
        return [], this_week, week_start, week_end

    # Fenêtre CONTINUE : finit sur la dernière semaine enregistrée, remonte ~WEEKS_WINDOW
    # semaines, trous comblés à 0 (1 point = 1 semaine, point le plus à droite = dernière semaine).
    right = data_weeks[-1]
    left  = max(data_weeks[0], right - datetime.timedelta(weeks=WEEKS_WINDOW - 1))
    sorted_weeks, wk = [], left
    while wk <= right:
        sorted_weeks.append((wk, {"km": km_by_week.get(wk, 0.0),
                                  "seances": seances_by_week.get(wk, 0),
                                  "start": wk}))
        wk += datetime.timedelta(weeks=1)
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
    ax.fill_between(x, kms, color=ORANGE, alpha=0.22, zorder=1)
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

    def __init__(self, row_index, km_col=COL_KM_JOUR):
        super().__init__()
        self.row_index = row_index
        self.km_col    = km_col   # colonne km_journee résolue par en-tête

    async def on_submit(self, interaction: discord.Interaction):
        r = str(self.ressentis); k = str(self.km).replace(",", ".")
        await asyncio.to_thread(write_cell, self.row_index, COL_RESSENTIS, r)
        await asyncio.to_thread(write_cell, self.row_index, self.km_col, k)
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Super, c'est noté ! 🎉\n"
                     f"**Ressentis :** {r}\n**Km :** {k} km\nBravo pour la séance ! 🏃‍♂️🔥"),
            view=None)


class SeanceView(ui.View):
    """Vue persistante : trouve la ligne du jour dynamiquement."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _today_row(self):
        today_str = date.today().isoformat()
        rows = await asyncio.to_thread(get_rows)
        row_idx = find_row(rows, today_str)
        return rows, row_idx

    @ui.button(label="✅ Séance faite", style=discord.ButtonStyle.success, custom_id="btn_valider")
    async def valider(self, interaction: discord.Interaction, button: ui.Button):
        rows, row_idx = await self._today_row()
        if row_idx is None:
            await interaction.response.send_message("❌ Aucune séance trouvée pour aujourd'hui.", ephemeral=True)
            return
        await interaction.response.send_modal(RessentisModal(row_idx, km_jour_col(rows)))

    @ui.button(label="❌ Non réalisée", style=discord.ButtonStyle.danger, custom_id="btn_non_realise")
    async def non_realise(self, interaction: discord.Interaction, button: ui.Button):
        rows, row_idx = await self._today_row()
        if row_idx is None:
            await interaction.response.send_message("❌ Aucune séance trouvée pour aujourd'hui.", ephemeral=True)
            return
        seance_txt = rows[row_idx][COL_SEANCE].strip() if len(rows[row_idx]) > COL_SEANCE else ""
        await asyncio.to_thread(write_cell, row_idx, COL_SEANCE,
                                f"{seance_txt} — ❌ Non réalisée")
        invalidate_seance_cache()
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Pas de souci, c'est noté ! 💪\n"
                     f"Séance marquée comme non réalisée. À demain !"),
            view=None)

# ── BOT ───────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── CACHE SÉANCE DU JOUR ─────────────────────────────────────────────────────
# Le process Railway est always-on : un dict en mémoire suffit. Premier appel de
# la journée → lecture du Sheet ; appels suivants → réponse immédiate. Le cache
# expire tout seul au changement de jour (clé = date) et est invalidé quand la
# séance du jour est réécrite (!programme, /programme, bouton ❌).
_seance_cache: dict = {}

def invalidate_seance_cache():
    _seance_cache.clear()

async def get_today_seance() -> dict:
    """Renvoie {"date", "row_idx", "seance", "jour"} pour aujourd'hui (avec cache)."""
    today_str = date.today().isoformat()
    if _seance_cache.get("date") == today_str:
        return _seance_cache
    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, today_str)
    info = {"date": today_str, "row_idx": row_idx, "seance": "", "jour": ""}
    if row_idx is not None:
        row = rows[row_idx]
        info["seance"] = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
        info["jour"]   = (row[COL_JOUR].strip() if len(row) > COL_JOUR else ""
                          ) or JOURS_FR[date.today().strftime("%A")]
    _seance_cache.clear()
    _seance_cache.update(info)
    return _seance_cache


async def build_seance_response():
    """Construit (content, view) du message de séance du jour. view=None si pas de boutons."""
    info    = await get_today_seance()
    date_fr = date.today().strftime("%d/%m/%Y")
    if info["row_idx"] is None:
        return (f"<@{DISCORD_USER_ID}> Aucune séance trouvée pour aujourd'hui ({info['date']}) 🤔", None)
    if not info["seance"]:
        return (f"📅 Aucune séance programmée aujourd'hui ({date_fr}).", None)
    content = (f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
               f"**📅 Séance — {info['jour']} {date_fr}**\n> **{info['seance']}**\n\n"
               f"Clique sur un bouton une fois ta séance terminée 👇")
    return (content, SeanceView())


async def send_seance_msg(channel):
    """Envoie le message de séance du jour avec boutons."""
    content, view = await build_seance_response()
    if view:
        await channel.send(content=content, view=view)
    else:
        await channel.send(content=content)


async def build_semaine_response() -> str:
    """Récap texte de toutes les séances de la semaine en cours (lundi → dimanche)."""
    rows  = await asyncio.to_thread(get_rows)
    kmc   = km_jour_col(rows)
    today = date.today()
    week_start = today - datetime.timedelta(days=today.weekday())   # lundi
    week_end   = week_start + datetime.timedelta(days=6)
    by_date = {row[0].strip(): row for row in rows[1:] if row and row[0].strip()}
    lines = []
    for i in range(7):
        d       = week_start + datetime.timedelta(days=i)
        row     = by_date.get(d.isoformat())
        jour_fr = JOURS_FR[d.strftime("%A")]
        seance  = row[COL_SEANCE].strip() if row and len(row) > COL_SEANCE else ""
        km      = row[kmc].strip()        if row and len(row) > kmc        else ""
        if not seance:
            body = "_rien de programmé_"
        elif "repos" in seance.lower():
            body = f"{seance} 😴"
        else:
            body = f"{seance} — ✅ {km} km" if km else seance
        marker = "👉 " if d == today else ""
        lines.append(f"{marker}**{jour_fr} {d.strftime('%d/%m')}** — {body}")
    header = (f"📅 **Tes séances — semaine du {week_start.strftime('%d/%m')} "
              f"au {week_end.strftime('%d/%m')}**")
    return header + "\n\n" + "\n".join(lines)


_tree_synced = False

@bot.event
async def on_ready():
    global _tree_synced
    print(f"✅ Connecté : {bot.user}")
    # on_ready peut être rappelé à chaque reconnexion Gateway → tout doit être idempotent
    if not weekly_summary_task.is_running(): weekly_summary_task.start()
    if not evening_check_task.is_running():  evening_check_task.start()
    if not coach_notes_task.is_running():    coach_notes_task.start()
    bot.add_view(SeanceView())
    bot.add_view(StartCoursesView())
    # Les slash commands ont été retirées (tout passe par !seance / !programme /
    # !claude) : on synchronise un arbre vide pour les désenregistrer de Discord.
    if not _tree_synced:
        try:
            channel = bot.get_channel(DISCORD_CHANNEL_ID)
            if channel and channel.guild:
                guild = discord.Object(id=channel.guild.id)
                bot.tree.clear_commands(guild=guild)
                await bot.tree.sync(guild=guild)
            bot.tree.clear_commands(guild=None)
            await bot.tree.sync()
            print("✅ Slash commands désenregistrées (menu / nettoyé)")
            _tree_synced = True
        except Exception as e:
            print(f"⚠️ Désenregistrement slash commands impossible : {e}")
    # Pré-chauffe le cache du jour pour que le premier /seance soit instantané
    try:
        await get_today_seance()
    except Exception as e:
        print(f"⚠️ Pré-chargement séance du jour : {e}")
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching,
                                  name="ton plan d'entraînement 🏃"))


@bot.command(name="seance")
async def cmd_seance(ctx, *, arg: str = ""):
    """!seance → séance du jour (boutons) · !seance semaine → récap de la semaine."""
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    try: await ctx.message.delete()
    except: pass
    if arg.strip().lower().startswith(("semaine", "sem", "week")):
        await ctx.channel.send(await build_semaine_response())
    else:
        await send_seance_msg(ctx.channel)


async def apply_programme(date_raw: str, seance: str) -> str:
    """Écrit une séance dans le planning, renvoie le message de confirmation/erreur."""
    date_iso = parse_date(date_raw)
    if not date_iso:
        return f"Date `{date_raw}` non reconnue. Essaie `16 juin`, `16/06` ou `2026-06-16`."
    rows    = await asyncio.to_thread(get_rows)
    row_idx = find_row(rows, date_iso)
    if row_idx is None:
        return f"Aucune ligne trouvée pour **{date_raw}** ({date_iso}) dans le planning."
    await asyncio.to_thread(write_cell, row_idx, COL_SEANCE, seance)
    if date_iso == date.today().isoformat():
        invalidate_seance_cache()
    try:
        d = date.fromisoformat(date_iso)
        affiche = f"{JOURS_FR[d.strftime('%A')]} {d.strftime('%d/%m/%Y')}"
    except: affiche = date_iso
    return f"✅ **{affiche}** → **{seance}**\nC'est noté dans ton plan 📋"


@bot.command(name="programme")
async def cmd_programme(ctx, *, args: str = ""):
    """!programme <date> : <séance> → modifie une ligne du planning."""
    if ctx.channel.id != DISCORD_CHANNEL_ID: return
    m = re.match(r"(.+?)\s*:\s*(.+)", args)
    if not m:
        await ctx.reply("Format : `!programme <date> : <séance>`\nEx: `!programme mardi 16 juin : footing 30min`")
        return
    await ctx.reply(await apply_programme(m.group(1).strip(), m.group(2).strip()))


COMMANDS_SHEET = "Commandes"

# Heures de passage (Paris) de la routine cloud qui traite l'onglet Commandes.
# À garder synchro avec le cron de la routine claude.ai : 45 7,12,17,18,21 * * *
ROUTINE_RUN_TIMES = (
    datetime.time(7, 45), datetime.time(12, 45), datetime.time(17, 45),
    datetime.time(18, 45), datetime.time(21, 45),
)

def next_routine_run() -> datetime.datetime:
    """Prochain passage de la routine cloud (datetime aware, Paris)."""
    now = datetime.datetime.now(PARIS)
    for t in ROUTINE_RUN_TIMES:
        run = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if run > now:
            return run
    t = ROUTINE_RUN_TIMES[0]
    return (now + datetime.timedelta(days=1)).replace(
        hour=t.hour, minute=t.minute, second=0, microsecond=0)


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
        run = int(next_routine_run().timestamp())
        await ctx.reply(
            f"✍️ Demande reçue ! Je la traite au prochain passage de la routine : "
            f"<t:{run}:t>, soit <t:{run}:R> ⏳"
        )
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
    kmc       = km_jour_col(rows)
    km_val    = row[kmc].strip()           if len(row) > kmc           else ""
    ressentis = row[COL_RESSENTIS].strip() if len(row) > COL_RESSENTIS else ""
    if km_val or ressentis or "Non réalisée" in seance_txt:
        return

    # Séance prévue mais pas encore renseignée → retag avec boutons
    view = SeanceView()
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


# ── TÂCHE 3 : NOTES DU COACH (Notes Nico) ────────────────────────────────────
# Prévient Loys à chaque NOUVELLE note du coach, une seule fois par ajout.
# L'état des notes déjà signalées vit dans un onglet CACHÉ (survit aux redémarrages).
# 1er passage = baseline silencieux : on mémorise l'existant sans notifier.
NOTES_STATE_SHEET = "Suivi Notes Coach"

def _notes_state_write(svc, snapshot: dict):
    """Réécrit l'onglet état (en-tête + {date: note})."""
    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{NOTES_STATE_SHEET}'!A:B").execute()
    values = [["Date", "Note connue"]] + [[d, n] for d, n in snapshot.items()]
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{NOTES_STATE_SHEET}'!A1",
        valueInputOption="RAW",
        body={"values": values}).execute()

def _coach_notes_step(current: dict):
    """Crée l'onglet état si besoin et renvoie (svc, initialise, stored{date:note}).
    initialise=False uniquement au tout 1er passage (baseline écrit en silence).
    Signal robuste : un onglet VIDE = pas encore initialisé (gère un crash éventuel
    entre création et baseline → on re-baseline sans spammer)."""
    svc  = sheets_svc()
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if NOTES_STATE_SHEET not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {
                "title": NOTES_STATE_SHEET, "hidden": True}}}]}).execute()
    res = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{NOTES_STATE_SHEET}'!A:B").execute()
    rows = res.get("values", [])
    if not rows:                       # onglet vide → baseline silencieux
        _notes_state_write(svc, current)
        return svc, False, dict(current)
    stored = {}
    for r in rows[1:]:
        if r and r[0].strip():
            stored[r[0].strip()] = r[1].strip() if len(r) > 1 else ""
    return svc, True, stored

async def _send_chunked(channel, header, lines, limit=1900):
    """Envoie header + lines en plusieurs messages si on dépasse la limite Discord."""
    msg = header
    for ln in lines:
        if len(msg) + 1 + len(ln) > limit:
            await channel.send(msg); msg = ln
        else:
            msg = f"{msg}\n{ln}"
    await channel.send(msg)

@tasks.loop(minutes=30)
async def coach_notes_task():
    """Toutes les 30 min : signale les nouvelles notes du coach (1 fois par ajout)."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return
    try:
        rows = await asyncio.to_thread(get_rows)
    except Exception as e:
        print(f"⚠️ Notes coach (lecture sheet) : {e}"); return
    nc = notes_nico_col(rows)
    current = {}
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        note = row[nc].strip() if len(row) > nc else ""
        if note:
            current[row[0].strip()] = note
    try:
        svc, initialise, stored = await asyncio.to_thread(_coach_notes_step, current)
    except Exception as e:
        print(f"⚠️ Notes coach (état) : {e}"); return

    if not initialise:   # 1er passage : message d'activation unique, pas de spam
        await channel.send(
            "👀 Surveillance des notes du coach (**Nico**) activée — "
            "je te préviens dès qu'une note est ajoutée. 📝")
        return

    new_items = sorted((d, n) for d, n in current.items() if stored.get(d) != n)
    if not new_items:
        if current != stored:   # note retirée par le coach → resync silencieux
            await asyncio.to_thread(_notes_state_write, svc, current)
        return

    lines = []
    for d, note in new_items:
        try:
            dd = date.fromisoformat(d)
            label = f"{JOURS_FR[dd.strftime('%A')]} {dd.strftime('%d/%m')}"
        except Exception:
            label = d
        note_disp = note if len(note) <= 400 else note[:399] + "…"
        lines.append(f"• **{label}** : {note_disp}")
    try:
        await _send_chunked(
            channel, f"📝 <@{DISCORD_USER_ID}> **Nouvelle(s) note(s) du coach Nico !**", lines)
    except Exception as e:
        # envoi échoué → on NE sauvegarde PAS l'état : re-tenté au prochain passage
        print(f"⚠️ Notes coach (envoi) : {e}"); return
    # envoi OK → on mémorise pour ne plus re-signaler ces notes
    await asyncio.to_thread(_notes_state_write, svc, current)

@coach_notes_task.before_loop
async def before_coach_notes():
    await bot.wait_until_ready()



# ── COURSES ───────────────────────────────────────────────────────────────────
COURSES_SHEET = "Courses"
MOIS_BOT = {
    "janvier":1,"février":2,"mars":3,"avril":4,"mai":5,"juin":6,
    "juillet":7,"août":8,"septembre":9,"octobre":10,"novembre":11,"décembre":12
}

def get_course_rows():
    r = sheets_svc().spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COURSES_SHEET}'!A:E"
    ).execute()
    return r.get("values", [])

def update_course_status(row_idx: int, status: str):
    sheets_svc().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{COURSES_SHEET}'!E{row_idx + 1}",
        valueInputOption="RAW",
        body={"values": [[status]]}
    ).execute()

def parse_race_date(date_txt: str):
    import re as _re
    for m_fr, m_num in MOIS_BOT.items():
        if m_fr in date_txt.lower():
            match = _re.search(r'(\d{1,2})', date_txt)
            if match:
                day = int(match.group(1))
                try:
                    d = date(date.today().year, m_num, day)
                    if d < date.today():
                        d = date(date.today().year + 1, m_num, day)
                    return d
                except Exception:
                    return None
    return None

def detect_conflicts(rows):
    """Retourne les paires (idx_a, nom_a, date_a_txt, idx_b, nom_b, date_b_txt) en conflit."""
    oui = []
    for i, row in enumerate(rows):
        if not row or len(row) < 5:
            continue
        if row[4].strip().lower() != "oui":
            continue
        d = parse_race_date(row[0].strip())
        if not d:
            continue
        nom = row[3].strip().split(" | ")[0]
        oui.append((i, nom, d, row[0].strip()))
    conflicts = []
    for a in range(len(oui)):
        for b in range(a + 1, len(oui)):
            if abs((oui[a][2] - oui[b][2]).days) < 7:
                conflicts.append((oui[a][0], oui[a][1], oui[a][3],
                                   oui[b][0], oui[b][1], oui[b][3]))
    return conflicts


class ConflictView(ui.View):
    def __init__(self, idx_a, nom_a, date_a_txt, idx_b, nom_b, date_b_txt):
        super().__init__(timeout=None)
        self.idx_a, self.nom_a, self.date_a_txt = idx_a, nom_a, date_a_txt
        self.idx_b, self.nom_b, self.date_b_txt = idx_b, nom_b, date_b_txt
        btn_a    = ui.Button(label=f"✅ {nom_a[:28]}", style=discord.ButtonStyle.success,
                             custom_id=f"conf_a_{idx_a}_{idx_b}")
        btn_b    = ui.Button(label=f"✅ {nom_b[:28]}", style=discord.ButtonStyle.primary,
                             custom_id=f"conf_b_{idx_a}_{idx_b}")
        btn_none = ui.Button(label="❌ Retirer les deux", style=discord.ButtonStyle.danger,
                             custom_id=f"conf_n_{idx_a}_{idx_b}")
        btn_a.callback    = self._keep_a
        btn_b.callback    = self._keep_b
        btn_none.callback = self._keep_none
        self.add_item(btn_a); self.add_item(btn_b); self.add_item(btn_none)

    async def _keep_a(self, interaction):
        await asyncio.to_thread(update_course_status, self.idx_b, "non")
        await interaction.response.edit_message(
            content=f"✅ **{self.nom_a}** gardée — **{self.nom_b}** retirée.", view=None)

    async def _keep_b(self, interaction):
        await asyncio.to_thread(update_course_status, self.idx_a, "non")
        await interaction.response.edit_message(
            content=f"✅ **{self.nom_b}** gardée — **{self.nom_a}** retirée.", view=None)

    async def _keep_none(self, interaction):
        await asyncio.to_thread(update_course_status, self.idx_a, "non")
        await asyncio.to_thread(update_course_status, self.idx_b, "non")
        await interaction.response.edit_message(
            content=f"❌ **{self.nom_a}** et **{self.nom_b}** retirées.", view=None)


async def _check_all_answered(channel):
    """Après chaque réponse, vérifie si tout est traité → détecte les conflits."""
    await asyncio.sleep(0.5)
    rows = await asyncio.to_thread(get_course_rows)
    still_pending = any(
        r and len(r) >= 5 and r[4].strip().lower() == "notifié"
        for r in rows
    )
    if still_pending:
        return  # il reste des courses sans réponse

    conflicts = detect_conflicts(rows)
    if conflicts:
        await channel.send(
            f"⚠️ <@{DISCORD_USER_ID}> **{len(conflicts)} conflit(s)** détecté(s) "
            f"— deux courses trop proches dans le temps !"
        )
        for (idx_a, nom_a, date_a_txt, idx_b, nom_b, date_b_txt) in conflicts:
            # Vérifier qu'elles sont toujours "oui" (un conflit précédent a pu les retirer)
            fresh = await asyncio.to_thread(get_course_rows)
            if (len(fresh) <= idx_a or fresh[idx_a][4].strip().lower() != "oui" or
                    len(fresh) <= idx_b or fresh[idx_b][4].strip().lower() != "oui"):
                continue
            d_a = parse_race_date(date_a_txt)
            d_b = parse_race_date(date_b_txt)
            delta = abs((d_a - d_b).days) if d_a and d_b else 0
            view = ConflictView(idx_a, nom_a, date_a_txt, idx_b, nom_b, date_b_txt)
            await channel.send(
                content=(f"⚠️ **{nom_a}** ({date_a_txt}) et **{nom_b}** ({date_b_txt}) "
                         f"ne sont qu'à **{delta} jour(s)** d'écart. Laquelle tu gardes ?"),
                view=view
            )
            await asyncio.sleep(1)
    else:
        accepted = sum(
            1 for r in rows
            if r and len(r) >= 5 and r[4].strip().lower() == "oui"
        )
        if accepted > 0:
            await channel.send(
                f"🎉 <@{DISCORD_USER_ID}> **{accepted} course(s)** validée(s), aucun conflit !\n"
                f"Dis **\"crée les rappels pour mes courses\"** sur Cowork pour programmer tes rappels 📅"
            )


class CourseView(ui.View):
    def __init__(self, row_index: int, nom: str):
        super().__init__(timeout=None)
        self.row_index = row_index
        self.nom       = nom
        btn_oui = ui.Button(label="👍 Oui, je m'inscris !",
                            style=discord.ButtonStyle.success,
                            custom_id=f"course_oui_{row_index}")
        btn_non = ui.Button(label="👎 Non merci",
                            style=discord.ButtonStyle.secondary,
                            custom_id=f"course_non_{row_index}")
        btn_oui.callback = self._oui
        btn_non.callback = self._non
        self.add_item(btn_oui); self.add_item(btn_non)

    async def _oui(self, interaction: discord.Interaction):
        await asyncio.to_thread(update_course_status, self.row_index, "oui")
        await interaction.response.edit_message(
            content=f"✅ **{self.nom}** — Noté, pense à t'inscrire ! 🏅", view=None)
        await _check_all_answered(interaction.channel)

    async def _non(self, interaction: discord.Interaction):
        await asyncio.to_thread(update_course_status, self.row_index, "non")
        await interaction.response.edit_message(
            content=f"❌ **{self.nom}** — Pas cette fois. 👍", view=None)
        await _check_all_answered(interaction.channel)


# ── PRÉSENTATION DES COURSES (déclenchée par bouton Discord) ─────────────────

async def present_courses_one_by_one(channel):
    """Envoie les courses 'nouveau' une par une avec boutons Oui/Non."""
    try:
        rows = await asyncio.to_thread(get_course_rows)
    except Exception as e:
        await channel.send(f"❌ Erreur lecture sheet : {e}")
        return

    nouveau = [
        (i, row) for i, row in enumerate(rows)
        if row and len(row) >= 5 and row[4].strip().lower() == "nouveau"
    ]
    if not nouveau:
        await channel.send(f"<@{DISCORD_USER_ID}> Aucune nouvelle course à présenter !")
        return

    for i, row in nouveau:
        date_txt = row[0].strip()
        lieux    = row[1].strip()
        distance = row[2].strip()
        info     = row[3].strip()
        nom, lien = info, ""
        if " | " in info:
            parts    = info.split(" | ", 1)
            nom, lien = parts[0].strip(), parts[1].strip()
        lien_txt = f"\n🔗 {lien}" if lien else ""
        content  = (
            f"🏃 <@{DISCORD_USER_ID}> Nouvelle course à proximité !\n\n"
            f"**{nom}**\n📅 {date_txt}\n📍 {lieux}\n🎽 {distance}{lien_txt}\n\n"
            f"Tu t'inscris ? 👇"
        )
        await asyncio.to_thread(update_course_status, i, "notifié")
        view = CourseView(i, nom)
        await channel.send(content=content, view=view)
        await asyncio.sleep(2)


class StartCoursesView(ui.View):
    """Bouton persistant : déclenche la présentation des courses."""
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🏃 Voir mes courses", style=discord.ButtonStyle.success,
               custom_id="start_courses_presentation")
    async def start_courses(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content="🏃 C'est parti ! Je t'envoie les courses une par une...",
            view=None
        )
        await present_courses_one_by_one(interaction.channel)


# ── LANCEMENT ─────────────────────────────────────────────────────────────────
# Doit rester en TOUT DERNIER : bot.run() est bloquant, donc toute classe/fonction
# définie après ne serait jamais chargée (cf. bug StartCoursesView).
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
