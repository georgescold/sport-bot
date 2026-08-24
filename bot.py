"""
bot.py - Bot Discord "Road to sub 38"
  !seance                  → séance du jour avec boutons (instantané, cache du jour)
  !seance semaine          → récap de toutes les séances de la semaine (lun→dim)
  !programme <date> : <s>  → modifie le planning
  !claude <demande>        → modification IA du sheet (langage naturel)
  Bouton ✅                → modale ressentis + km
  Bouton ❌                → séance non réalisée
  8h00 quotidien           → séance du jour + note du coach + boutons de validation
                             (jour de repos → propose une séance de muscu à saisir)
  toutes les 30 min        → prévient si le coach modifie la journée après 8h00
  21h05 quotidien          → rappel si séance non renseignée (ou msg neutre si vide)
  21h00 dimanche           → résumé hebdomadaire + graphique Strava-like

Le suivi du second athlète (Enzo, musculation) vit dans enzo.py : autre classeur,
autres colonnes, custom_id préfixés, aucun croisement possible avec cette feuille.

La note du coach (colonne « Notes Nico ») accompagne la séance partout où celle-ci
est affichée, plutôt que dans un message séparé.
"""

import os, re, json, asyncio, io, datetime
from datetime import date
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from discord import ui
from google.oauth2 import service_account
from googleapiclient.discovery import build

try:
    import enzo                      # suivi du second athlete, module independant
except Exception as _e:              # pragma: no cover
    enzo = None
    print(f"⚠️ Module enzo indisponible : {_e}")

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
# G="Notes Nico" (colonne du COACH : NE JAMAIS écrire) H=km_journee I=km_semaine
COL_DATE       = 0   # A
COL_JOUR       = 1   # B
COL_SEANCE     = 2   # C
COL_TYPE       = 4   # E : type de séance (déduit de C si vide ; coach prioritaire)
COL_RESSENTIS  = 5   # F
COL_NOTES_NICO = 6   # G : coach, lecture seule
COL_KM_JOUR    = 7   # H : fallback ; la vraie colonne est résolue par km_jour_col()

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

# FR jour → numéro de weekday (0 = lundi) pour reconstruire les dates manquantes.
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

def backfill_types(rows, persist: bool = True) -> int:
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
        if cur:                        # déjà rempli (coach) → on respecte
            continue
        typ = seance_type(seance)
        if not typ:
            continue
        _row_set(row, COL_TYPE, typ)
        filled += 1
        if persist:
            try: write_cell(i, COL_TYPE, typ)
            except Exception as e: print(f"⚠️ backfill type ligne {i+1} : {e}")
    return filled

def backfill_dates(rows, persist: bool = True) -> int:
    """Complète les dates (col A) ET les jours (col B) manquants, et les écrit
    dans le Sheet.

    Le coach remplit parfois la séance (col C) du planning à venir mais laisse la
    date (A) et/ou le jour (B) vides. Sans date, find_row() ne retrouve pas la
    ligne du jour → « Aucune séance trouvée » et les boutons n'ont aucune ligne
    où écrire (rien n'arrive dans le Drive). Reconstruction :
      • jour manquant mais date présente   → jour déduit de la date ;
      • date manquante mais jour présent   → 1ʳᵉ date après la dernière connue
                                             dont le weekday == col B ;
      • date ET jour manquants (séance présente) → jour consécutif suivant.
    Les lignes entièrement vides (ni jour ni séance) sont ignorées (espaceurs /
    bas de planning). Modifie `rows` en place ; renvoie le nb de lignes touchées."""
    last_date = None
    filled = 0
    for i, row in enumerate(rows):
        if i == 0 or not row:
            continue
        a = row[0].strip() if len(row) > 0 else ""
        b = row[1].strip() if len(row) > 1 else ""
        c = row[2].strip() if len(row) > 2 else ""
        if a:                                   # ligne datée = ancre
            try: last_date = date.fromisoformat(a)
            except ValueError: continue
            if not b:                           # jour manquant → déduit de la date
                jr = JOURS_FR[last_date.strftime("%A")]
                _row_set(row, COL_JOUR, jr); filled += 1
                if persist:
                    try: write_cell(i, COL_JOUR, jr)
                    except Exception as e: print(f"⚠️ backfill jour ligne {i+1} : {e}")
            continue
        if last_date is None or (not b and not c):   # pas d'ancre / ligne vide
            continue
        wd = JOUR_TO_WD.get(b.lower()) if b else None
        cand = last_date
        if wd is not None:                      # jour connu → date du bon weekday
            for _ in range(7):
                cand += datetime.timedelta(days=1)
                if cand.weekday() == wd:
                    break
            else:
                continue
        else:                                   # jour absent → jour suivant consécutif
            cand += datetime.timedelta(days=1)
        _row_set(row, COL_DATE, cand.isoformat())
        if persist:
            try: write_cell(i, COL_DATE, cand.isoformat(), parse=True)
            except Exception as e: print(f"⚠️ backfill date ligne {i+1} : {e}")
        if not b:                               # jour aussi manquant → déduit
            jr = JOURS_FR[cand.strftime("%A")]
            _row_set(row, COL_JOUR, jr)
            if persist:
                try: write_cell(i, COL_JOUR, jr)
                except Exception as e: print(f"⚠️ backfill jour ligne {i+1} : {e}")
        last_date = cand
        filled   += 1
    return filled

def write_cell(row, col, val, parse: bool = False):
    """Écrit une cellule. parse=True → USER_ENTERED : Sheets interprète la valeur
    (nombre, date) au lieu de la stocker en texte.

    Indispensable pour la date (col A) et les km : en RAW, "4.22" est stocké comme
    TEXTE, or SUMIFS/SUM ignorent le texte → les colonnes d'agrégat I..L
    (Km_semaine / 7j / mois / annuel) sous-compteraient silencieusement.
    Laisser parse=False pour tout texte libre : un ressenti ou une séance
    commençant par « = » serait sinon interprété comme une formule."""
    sheets_svc().spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET_NAME}'!{chr(65+col)}{row+1}",
        valueInputOption="USER_ENTERED" if parse else "RAW",
        body={"values": [[val]]}).execute()

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


def note_coach(rows, row_idx) -> str:
    """Note du coach (colonne « Notes Nico ») pour une ligne donnée. '' si absente."""
    if row_idx is None or row_idx >= len(rows):
        return ""
    nc  = notes_nico_col(rows)
    row = rows[row_idx]
    return row[nc].strip() if len(row) > nc else ""


def bloc_note(note: str, limite: int = 900) -> str:
    """Bloc Discord de la note du coach, à coller sous une séance. '' si pas de note.

    Chaque ligne est préfixée par « > » : les notes de Nico sont souvent
    multi-lignes, et un seul « > » en tête ne citerait que la première ligne.
    La limite protège le plafond de 2000 caractères d'un message Discord."""
    if not note:
        return ""
    if len(note) > limite:
        note = note[:limite - 1] + "…"
    cite = "\n".join(f"> {ligne}" for ligne in note.splitlines())
    return f"\n\n📝 **Note du coach Nico**\n{cite}"


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
        lines.append(f"{date_s} | {jour} | Séance: {seance or '-'} | Ressentis: {ress or '-'} | Km: {km or '-'}")
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
        is_rest = est_repos(seance) or "Non réalisée" in seance
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
        await asyncio.to_thread(write_cell, self.row_index, self.km_col, k, True)
        await interaction.response.edit_message(
            content=(f"<@{DISCORD_USER_ID}> Super, c'est noté ! 🎉\n"
                     f"**Ressentis :** {r}\n**Km :** {k} km\nBravo pour la séance ! 🏃‍♂️🔥"),
            view=None)


# ── CE QUI A ÉTÉ ANNONCÉ SUR DISCORD AUJOURD'HUI ─────────────────────────────
# Photo de la séance (col C) et de la consigne du coach (col G) au moment où le
# bot les a annoncées. seance_watch_task compare le Sheet à cette photo : si Nico
# retouche la journée après le rappel de 8h00, Loys est prévenu au lieu de courir
# la mauvaise séance. En mémoire seulement : après un redéploiement, le premier
# passage se contente de reprendre la photo, sans notifier.
_annonce: dict = {}

def set_annonce(seance: str, note: str = "", d: str = None):
    """Mémorise l'état annoncé. À appeler APRÈS chaque écriture du bot en col C,
    sinon la surveillance prendrait le bot lui-même pour le coach."""
    _annonce.update({"date":   d or date.today().isoformat(),
                     "seance": (seance or "").strip(),
                     "note":   (note or "").strip()})


def est_repos(seance: str) -> bool:
    """Journée sans séance de course programmée (case vide ou repos)."""
    return (not (seance or "").strip()) or seance_type(seance) == "Repos"


class MuscuModal(ui.Modal, title="💪 Ta séance de muscu"):
    """Jour de repos : Loys saisit lui-même la séance qu'il veut faire."""
    contenu = ui.TextInput(label="Ton programme",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: Pompes 3x15, DC barre 4x10 (récup 3'), tractions 4x8, gainage 3x1'",
        min_length=3, max_length=1000)

    async def on_submit(self, interaction: discord.Interaction):
        today_str = date.today().isoformat()
        rows      = await asyncio.to_thread(get_rows)
        row_idx   = find_row(rows, today_str)
        if row_idx is None:
            await interaction.response.send_message(
                "❌ Aucune ligne pour aujourd'hui dans le planning, impossible d'écrire.",
                ephemeral=True)
            return
        # Le mot « repos » est volontairement remplacé : la journée devient une
        # vraie séance pour tout le reste du bot (relance 21h05, récap hebdo,
        # graphe) qui teste la présence de « repos » dans la col C.
        texte = f"renfo muscu : {str(self.contenu).strip()}"
        await asyncio.to_thread(write_cell, row_idx, COL_SEANCE, texte)
        await asyncio.to_thread(write_cell, row_idx, COL_TYPE, "Renfo")
        invalidate_seance_cache()
        set_annonce(texte, _annonce.get("note", ""))
        jour = JOURS_FR[date.today().strftime("%A")]
        await interaction.response.edit_message(
            content=(f"💪 <@{DISCORD_USER_ID}> C'est noté dans le planning !\n\n"
                     f"**📅 Séance : {jour} {date.today().strftime('%d/%m/%Y')}**\n"
                     f"> **{texte}**\n\n"
                     f"Clique sur un bouton une fois la séance terminée 👇"),
            view=SeanceView())


class ReposView(ui.View):
    """Vue persistante des jours de repos : propose d'ajouter une muscu."""
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="💪 Je fais une muscu", style=discord.ButtonStyle.primary,
               custom_id="btn_repos_muscu")
    async def muscu(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.send_modal(MuscuModal())

    @ui.button(label="🛋️ Vraie récup", style=discord.ButtonStyle.secondary,
               custom_id="btn_repos_off")
    async def recup(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(
            content=(f"🛋️ <@{DISCORD_USER_ID}> Repos complet aujourd'hui, "
                     f"la récup fait partie du plan. À demain !"),
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
                                f"{seance_txt} - ❌ Non réalisée")
        invalidate_seance_cache()
        set_annonce(f"{seance_txt} - ❌ Non réalisée", _annonce.get("note", ""))
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
    await asyncio.to_thread(backfill_dates, rows)   # complète les dates col A manquantes
    await asyncio.to_thread(backfill_types, rows)   # remplit la col E (type) si vide
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
    # Note du coach relue à chaque appel, JAMAIS mise en cache : le cache de séance
    # n'est invalidé que quand le bot modifie lui-même la séance, or Nico peut
    # ajouter une note dans le sheet à n'importe quel moment de la journée.
    rows = await asyncio.to_thread(get_rows)
    note = await asyncio.to_thread(note_coach, rows, info["row_idx"])
    if est_repos(info["seance"]):
        return ((f"😴 <@{DISCORD_USER_ID}> Aujourd'hui c'est repos !\n\n"
                 f"**📅 Repos : {info['jour']} {date_fr}**"
                 f"{bloc_note(note)}\n\n"
                 f"Tu veux quand même faire une séance de muscu ? 👇"), ReposView())
    content = (f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
               f"**📅 Séance : {info['jour']} {date_fr}**\n> **{info['seance']}**"
               f"{bloc_note(note)}\n\n"
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
    nc    = notes_nico_col(rows)
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
            body = f"{seance} (✅ {km} km)" if km else seance
        marker = "👉 " if d == today else ""
        lines.append(f"{marker}**{jour_fr} {d.strftime('%d/%m')}** : {body}")
        # Note du coach en version compacte : sur 7 jours, les notes intégrales
        # feraient exploser le plafond de 2000 caractères d'un message Discord.
        note = row[nc].strip() if row and len(row) > nc else ""
        if note:
            court = " ".join(note.split())
            if len(court) > 160:
                court = court[:159] + "…"
            lines.append(f"　　📝 _{court}_")
    header = (f"📅 **Tes séances de la semaine du {week_start.strftime('%d/%m')} "
              f"au {week_end.strftime('%d/%m')}**")
    msg = header + "\n\n" + "\n".join(lines)
    if len(msg) > 1990:                      # filet de sécurité : Discord rejette au-delà de 2000
        msg = msg[:1989] + "…"
    return msg


_tree_synced = False

@bot.event
async def on_ready():
    global _tree_synced
    print(f"✅ Connecté : {bot.user}")
    # on_ready peut être rappelé à chaque reconnexion Gateway → tout doit être idempotent
    if not weekly_summary_task.is_running(): weekly_summary_task.start()
    if not evening_check_task.is_running():  evening_check_task.start()
    if not morning_reminder_task.is_running(): morning_reminder_task.start()
    if not seance_watch_task.is_running(): seance_watch_task.start()
    bot.add_view(SeanceView())
    bot.add_view(ReposView())
    if enzo:
        try:
            enzo.init(bot, prochain_run=next_routine_run)
        except Exception as e:
            print(f"⚠️ Demarrage du suivi d'Enzo : {e}")
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


# ── AIGUILLAGE DES COMMANDES PAR SALON ───────────────────────────────────────
# Une commande tapee dans le salon d'un athlete ne doit toucher QUE cet athlete.
# salon_de(ctx) renvoie "loys", "enzo", ou None si la commande vient d'ailleurs
# (auquel cas le bot reste muet). C'est le seul endroit ou l'on decide a qui
# appartient une commande : chaque commande l'appelle en premiere ligne.
def salon_de(ctx):
    if ctx.channel.id == DISCORD_CHANNEL_ID:
        return "loys"
    if enzo and ctx.channel.id == enzo.CHANNEL_ID:
        return "enzo"
    return None


@bot.command(name="seance")
async def cmd_seance(ctx, *, arg: str = ""):
    """!seance → séance du jour (boutons) · !seance semaine → récap de la semaine.
    Fonctionne dans les deux salons, chacun sur SON planning."""
    salon = salon_de(ctx)
    if salon is None: return
    try: await ctx.message.delete()
    except: pass
    semaine = arg.strip().lower().startswith(("semaine", "sem", "week"))
    if salon == "enzo":
        if semaine:
            await enzo.commande_semaine(ctx.channel)
        else:
            await enzo.envoyer_seance(ctx.channel, rappel=False)
        return
    if semaine:
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
        set_annonce(seance, _annonce.get("note", ""))
    try:
        d = date.fromisoformat(date_iso)
        affiche = f"{JOURS_FR[d.strftime('%A')]} {d.strftime('%d/%m/%Y')}"
    except: affiche = date_iso
    return f"✅ **{affiche}** → **{seance}**\nC'est noté dans ton plan 📋"


@bot.command(name="programme")
async def cmd_programme(ctx, *, args: str = ""):
    """!programme <date> : <séance> → modifie une ligne du planning.
    Dans le salon d'Enzo, ecrit dans SON planning et seul le coach y a droit."""
    salon = salon_de(ctx)
    if salon is None: return
    m = re.match(r"(.+?)\s*:\s*(.+)", args)
    if not m:
        await ctx.reply("Format : `!programme <date> : <séance>`\nEx: `!programme mardi 16 juin : footing 30min`")
        return
    if salon == "enzo":
        await enzo.commande_programme(ctx.channel, ctx.author.id,
                                      m.group(1).strip(), m.group(2).strip())
        return
    await ctx.reply(await apply_programme(m.group(1).strip(), m.group(2).strip()))


COMMANDS_SHEET = "Commandes"

# Heures de passage (Paris) de la routine cloud qui traite l'onglet Commandes.
# À garder synchro avec le cron de la routine claude.ai : 45 7,12,17,18,21 * * *
# (Ces passages ne servent plus qu'à l'onglet Commandes : le rappel de séance est
#  désormais envoyé par morning_reminder_task à 8h00, cf. plus bas.)
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
    """!claude <demande> → enregistre dans l'onglet Commandes pour traitement Cowork.
    Dans le salon d'Enzo, la demande part dans SON onglet Commandes."""
    salon = salon_de(ctx)
    if salon is None: return
    if salon == "enzo":
        await enzo.commande_claude(ctx.channel, ctx.author.id, request)
        return
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
    """Rappel si séance non renseignée ; message neutre si aucune séance programmée."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return

    today_str = date.today().isoformat()
    date_fr   = date.today().strftime("%d/%m/%Y")
    today_day = JOURS_FR[date.today().strftime("%A")]

    rows    = await asyncio.to_thread(get_rows)
    await asyncio.to_thread(backfill_dates, rows)   # complète les dates col A manquantes
    await asyncio.to_thread(backfill_types, rows)   # remplit la col E (type) si vide
    row_idx = find_row(rows, today_str)

    # Pas de ligne pour aujourd'hui
    if row_idx is None:
        await channel.send(f"📅 Aucune séance programmée aujourd'hui ({date_fr}).")
        return

    row        = rows[row_idx]
    seance_txt = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""

    # Case vide ou repos → message neutre, pas de tag
    if est_repos(seance_txt):
        await channel.send(f"😴 Pas de séance programmée aujourd'hui, bonne récupération ! 🛋️")
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
                 f"**📅 Rappel : {today_day} {date_fr}**\n> **{seance_txt}**"
                 f"{bloc_note(note_coach(rows, row_idx))}\n\n"
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

    texte = (f"📊 **Résumé de la semaine du {debut} au {fin}**\n\n"
             f"🏃 **{km:.1f} km** parcourus cette semaine\n"
             f"✅ **{nb} séance(s)** réalisée(s)\n\n"
             f"<@{DISCORD_USER_ID}> Belle semaine, continue comme ça ! 💪")

    buf  = await asyncio.to_thread(generate_chart, sorted_weeks)
    file = discord.File(buf, filename="progression_km.png")
    await channel.send(content=texte, file=file)


@weekly_summary_task.before_loop
async def before_weekly():
    await bot.wait_until_ready()


# L'ancienne TÂCHE 3 envoyait la note du coach seule à 6h00. Supprimée : la note
# est désormais jointe à la séance elle-même (rappel 8h, rappel 21h05, !seance et
# !seance semaine), donc lue dans son contexte plutôt que 2h avant. Cf. bloc_note().


# ── TÂCHE 3 : RAPPEL DE SÉANCE DU MATIN : 8h00 ───────────────────────────────
# Anciennement envoyé par la routine cloud claude.ai (`sport_bot.py --reminder`,
# fenêtre 17h-18h). Rapatrié ici : le bot Railway tourne déjà 24/7, c'est plus
# fiable et la planification est versionnée avec le code.
# ⚠️ L'étape --reminder doit rester désactivée côté routine, sinon double envoi.
@tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=PARIS))
async def morning_reminder_task():
    """8h00 chaque jour : annonce la séance du jour avec les boutons de validation."""
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return
    try:
        rows = await asyncio.to_thread(get_rows)
        await asyncio.to_thread(backfill_dates, rows)   # complète les dates col A/B manquantes
        await asyncio.to_thread(backfill_types, rows)   # remplit la col E (type) si vide
    except Exception as e:
        print(f"⚠️ Rappel matin (lecture sheet) : {e}"); return

    today_str = date.today().isoformat()
    row_idx   = find_row(rows, today_str)
    if row_idx is None:
        print(f"⚠️ Rappel matin : aucune ligne pour {today_str}"); return

    row     = rows[row_idx]
    seance  = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else "Repos"
    jour    = row[COL_JOUR].strip()   if len(row) > COL_JOUR   else JOURS_FR[date.today().strftime("%A")]
    date_fr = date.today().strftime("%d/%m/%Y")

    note = note_coach(rows, row_idx)   # consigne du coach livrée AVEC la séance
    if est_repos(seance):
        # Jour de repos : on propose une muscu plutôt que d'envoyer un rappel vide.
        content = (f"😴 <@{DISCORD_USER_ID}> Aujourd'hui c'est repos !\n\n"
                   f"**📅 Repos : {jour} {date_fr}**"
                   f"{bloc_note(note)}\n\n"
                   f"Tu veux quand même faire une séance de muscu ? 👇")
        view = ReposView()
    else:
        content = (f"🏃 Hey <@{DISCORD_USER_ID}> ! N'oublie pas de t'entraîner ! 💪\n\n"
                   f"**📅 Séance : {jour} {date_fr}**\n"
                   f"> **{seance}**"
                   f"{bloc_note(note)}\n\n"
                   f"Clique sur un bouton une fois ta séance terminée 👇")
        view = SeanceView()
    try:
        await channel.send(content=content, view=view)
        set_annonce(seance, note)   # point de départ de la surveillance du jour
    except Exception as e:
        print(f"⚠️ Rappel matin (envoi) : {e}")


@morning_reminder_task.before_loop
async def before_morning_reminder():
    await bot.wait_until_ready()


# ── TÂCHE 4 : LE COACH A-T-IL MODIFIÉ LA JOURNÉE ? (toutes les 30 min) ───────
WATCH_DEBUT, WATCH_FIN = 9, 21        # fenêtre de surveillance, heures de Paris

@tasks.loop(minutes=30)
async def seance_watch_task():
    """Prévient Loys si la séance du jour (col C) ou la consigne du coach (col G)
    change APRÈS le rappel de 8h00. Nico retouche parfois le planning en cours de
    journée : sans ça, Loys part sur la séance annoncée le matin."""
    now = datetime.datetime.now(PARIS)
    if not (WATCH_DEBUT <= now.hour < WATCH_FIN):
        return
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel: return

    today_str = date.today().isoformat()
    try:
        rows = await asyncio.to_thread(get_rows)
    except Exception as e:
        print(f"⚠️ Surveillance séance (lecture sheet) : {e}"); return
    row_idx = find_row(rows, today_str)
    if row_idx is None: return

    row    = rows[row_idx]
    seance = row[COL_SEANCE].strip() if len(row) > COL_SEANCE else ""
    note   = note_coach(rows, row_idx)

    # Premier passage de la journée (ou après un redéploiement) : on prend la
    # photo sans rien annoncer, on ne sait pas ce qui a changé avant.
    if _annonce.get("date") != today_str:
        set_annonce(seance, note, today_str)
        return

    avant, avant_note = _annonce.get("seance", ""), _annonce.get("note", "")
    if seance == avant and note == avant_note:
        return

    set_annonce(seance, note, today_str)
    invalidate_seance_cache()

    blocs = []
    if seance != avant:
        blocs.append(f"**La séance a changé**\n"
                     f"> ~~{avant or 'case vide'}~~\n"
                     f"> **{seance or 'case vide'}**")
    if note != avant_note:
        blocs.append("**Nouvelle consigne du coach**" +
                     (bloc_note(note) or f"\n> _consigne retirée_"))
    heure = now.strftime("%Hh%M")
    await channel.send(
        content=(f"✏️ <@{DISCORD_USER_ID}> Nico a modifié ta journée depuis le "
                 f"rappel de 8h00 (vu à {heure}) !\n\n" + f"\n\n".join(blocs)),
        view=ReposView() if est_repos(seance) else SeanceView())


@seance_watch_task.before_loop
async def before_seance_watch():
    await bot.wait_until_ready()


# ── LANCEMENT ─────────────────────────────────────────────────────────────────
# Doit rester en TOUT DERNIER : bot.run() est bloquant, donc toute classe/fonction
# définie après ne serait jamais chargée.
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
