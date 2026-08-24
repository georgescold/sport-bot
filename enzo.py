"""
enzo.py - Suivi de l'athlete Enzo (musculation debutant), branche sur bot.py.

Volontairement SEPARE de bot.py : le planning d'Enzo vit dans un autre classeur,
n'a pas les memes colonnes que celui de Loys, et un clic sur un de ses boutons ne
doit jamais pouvoir ecrire dans la feuille de Loys. Tous les custom_id sont donc
prefixes "enzo_", et ce module n'importe PAS bot.py (pas d'import circulaire) :
bot.py appelle simplement init(bot) au demarrage.

  8h00 quotidien   -> seance du jour dans #sport-enzo + boutons de validation
  21h05 quotidien  -> relance si la seance n'est pas renseignee
  toutes les 20 min -> propositions du coach en attente de validation par Loys

Le coach IA ne passe PAS par l'API Anthropic : le bot depose une demande dans
l'onglet « Commandes » du classeur d'Enzo, et la routine cloud claude.ai la
traite au prochain passage (elle tourne sous l'abonnement de Loys).
"""

import os, re, json, asyncio, datetime
from datetime import date
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from discord import ui
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ── CONFIGURATION ─────────────────────────────────────────────────────────────
CHANNEL_ID     = int(os.environ.get("ENZO_CHANNEL_ID", "1541366095917879347"))
USER_ID        = int(os.environ.get("ENZO_USER_ID", "200307038947901441"))
SPREADSHEET_ID = os.environ.get("ENZO_SPREADSHEET_ID",
                                "14Ec9DwKGXpULMeHRmn4AE5LILYG4bzEd5AaN6e7bDyo")
SHEET          = "Données Enzo"
CMD_SHEET      = "Commandes"
PROP_SHEET     = "Propositions"
SA_JSON        = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]

# Loys est le coach : lui seul valide les adaptations proposees.
COACH_ID = int(os.environ.get("DISCORD_USER_ID", "0"))

PARIS = ZoneInfo("Europe/Paris")

def aujourdhui():
    """Le jour vecu par l'athlete, pas celui du serveur. Railway tourne en UTC :
    entre minuit et 2h du matin a Paris, date.today() renverrait encore la veille."""
    return datetime.datetime.now(PARIS).date()

# Colonnes de « Données Enzo » (0-based). Differentes de celles de Loys.
COL_DATE, COL_JOUR, COL_MOIS   = 0, 1, 2
COL_SEANCE, COL_DOUBLE         = 3, 4
COL_TYPE, COL_RESSENTIS        = 5, 6
COL_KM                         = 7
COL_BLOC                       = 12
COL_CHARGES, COL_RPE, COL_COACH = 13, 14, 15

JOURS_FR = {"Monday":"Lundi","Tuesday":"Mardi","Wednesday":"Mercredi","Thursday":"Jeudi",
            "Friday":"Vendredi","Saturday":"Samedi","Sunday":"Dimanche"}

_bot = None          # instance discord.ext.commands.Bot, fournie par bot.py


# ── ACCES AU CLASSEUR ─────────────────────────────────────────────────────────
def _svc():
    creds = service_account.Credentials.from_service_account_info(
        json.loads(SA_JSON), scopes=["https://www.googleapis.com/auth/spreadsheets"])
    return build("sheets", "v4", credentials=creds).spreadsheets()

def get_rows():
    return _svc().values().get(
        spreadsheetId=SPREADSHEET_ID, range=f"'{SHEET}'!A:P").execute().get("values", [])

def write_cell(row, col, val, parse=False):
    """Ecrit une cellule. parse=True -> USER_ENTERED (nombres et dates reels).

    Le classeur est en locale en_US : le separateur decimal est le POINT. En
    fr_FR, « 3.5 » serait stocke comme la date du 3 mai et fausserait tous les
    agregats de kilometres. Ne jamais rebasculer ce classeur en fr_FR."""
    _svc().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{SHEET}'!{chr(65 + col)}{row + 1}",
        valueInputOption="USER_ENTERED" if parse else "RAW",
        body={"values": [[val]]}).execute()

def find_row(rows, d_iso):
    for i, r in enumerate(rows):
        if r and r[0].strip() == d_iso:
            return i
    return None

def cell(row, col):
    return row[col].strip() if len(row) > col else ""

def est_repos(seance):
    return (not seance.strip()) or seance.strip().lower().startswith("repos")

def est_course(type_seance):
    return type_seance.strip().lower() in ("footing", "course")


# ── MISE EN FORME DU MESSAGE ──────────────────────────────────────────────────
# La seance est stockee en colonne D sous la forme :
#   "Haut A : Développé couché|4x10|3-4|2min · Tirage vertical|4x10|3-4|2min · ..."
# soit un titre, puis des exercices separes par « · », chacun portant ses quatre
# champs separes par « | ». On la rend sous forme de tableau aligne dans un bloc
# de code Discord : c'est la seule mise en forme a chasse fixe disponible, et
# c'est ce qui se lit le mieux telephone en main pendant la seance.
# Discord sur telephone ne fait PAS defiler les blocs de code : il les renvoie a
# la ligne, ce qui disloque un tableau en colonnes. Un vrai tableau aligne tient
# en 49 caracteres, or l'ecran d'un telephone en affiche une trentaine. On passe
# donc a deux lignes par exercice : le nom, puis ses chiffres en dessous. Rien ne
# depasse ~30 caracteres, donc rien ne se replie, et la lecture reste verticale.
LARGEUR_MAX = 30

def _propre(txt):
    # Un accent grave fermerait le bloc de code et disloquerait tout le message.
    return txt.strip().replace("`", "'")

def tableau_seance(seance: str) -> str:
    seance = (seance or "").strip()
    if not seance:
        return ""
    titre, _, reste = seance.partition(" : ")
    exos = [e.strip() for e in reste.split(" · ") if e.strip()] if reste else []
    if not exos or not any("|" in e for e in exos):
        return f"> **{_propre(seance)}**"

    lignes = []
    for i, e in enumerate(exos, 1):
        ch = [_propre(c) for c in e.split("|")]
        ch += [""] * (4 - len(ch))
        nom, series, rpe, recup = ch[0], ch[1], ch[2], ch[3]
        details = [d for d in (series, f"RPE {rpe}" if rpe else "", recup) if d]
        lignes.append(f"{i}. {nom}")
        lignes.append("   " + "  ".join(details))
    corps = "\n".join(lignes)
    return f"**{titre}**\n```\n{corps}\n```"


def resume_seance(seance: str, limite: int = 90) -> str:
    """Version courte, une ligne, pour les messages qui ne sont pas la seance."""
    titre, _, reste = (seance or "").partition(" : ")
    if not reste:
        return (seance or "")[:limite]
    noms = [e.split("|")[0].strip() for e in reste.split(" · ") if e.strip()]
    txt = f"{titre} : " + ", ".join(noms)
    return txt[:limite] + ("..." if len(txt) > limite else "")


# ── SAISIE DE LA SEANCE ───────────────────────────────────────────────────────
class MuscuModal(ui.Modal, title="💪 Ta séance de muscu"):
    charges = ui.TextInput(label="Charges utilisées",
        placeholder="Ex: DC 25 kg, tirage 30 kg, curl 12 kg", max_length=200)
    rpe = ui.TextInput(label="RPE ressenti (1 à 5)",
        placeholder="4", max_length=3)
    ressentis = ui.TextInput(label="Ressentis, douleurs, remarques",
        style=discord.TextStyle.paragraph,
        placeholder="Ex: bonne séance, gêne au genou sur les fentes", max_length=500)

    def __init__(self, row_idx):
        super().__init__()
        self.row_idx = row_idx

    async def on_submit(self, interaction: discord.Interaction):
        c, r, s = str(self.charges).strip(), str(self.rpe).strip(), str(self.ressentis).strip()
        await asyncio.to_thread(write_cell, self.row_idx, COL_CHARGES, c)
        await asyncio.to_thread(write_cell, self.row_idx, COL_RPE, r.replace(",", "."), True)
        await asyncio.to_thread(write_cell, self.row_idx, COL_RESSENTIS, s)
        await interaction.response.edit_message(
            content=(f"✅ <@{USER_ID}> C'est enregistré, bravo pour la séance ! 🔥\n"
                     f"**Charges :** {c}\n**RPE :** {r}/5\n**Ressentis :** {s}"),
            view=None)
        await verifier_declencheur_coach(self.row_idx, interaction.channel)


class CourseModal(ui.Modal, title="🏃 Ta sortie du jour"):
    km = ui.TextInput(label="Kilomètres parcourus", placeholder="Ex: 2.4", max_length=10)
    rpe = ui.TextInput(label="RPE ressenti (1 à 5)", placeholder="3", max_length=3)
    ressentis = ui.TextInput(label="Ressentis, douleurs, remarques",
        style=discord.TextStyle.paragraph, max_length=500)

    def __init__(self, row_idx):
        super().__init__()
        self.row_idx = row_idx

    async def on_submit(self, interaction: discord.Interaction):
        k = str(self.km).strip().replace(",", ".")
        r, s = str(self.rpe).strip(), str(self.ressentis).strip()
        await asyncio.to_thread(write_cell, self.row_idx, COL_KM, k, True)
        await asyncio.to_thread(write_cell, self.row_idx, COL_RPE, r.replace(",", "."), True)
        await asyncio.to_thread(write_cell, self.row_idx, COL_RESSENTIS, s)
        await interaction.response.edit_message(
            content=(f"✅ <@{USER_ID}> Sortie enregistrée ! 🏃\n"
                     f"**Distance :** {k} km\n**RPE :** {r}/5\n**Ressentis :** {s}"),
            view=None)
        await verifier_declencheur_coach(self.row_idx, interaction.channel)


class SeanceView(ui.View):
    """Vue persistante d'Enzo. custom_id prefixes pour ne jamais croiser ceux de Loys."""
    def __init__(self):
        super().__init__(timeout=None)

    async def _ligne_du_jour(self):
        rows = await asyncio.to_thread(get_rows)
        return rows, find_row(rows, aujourdhui().isoformat())

    @ui.button(label="✅ Séance faite", style=discord.ButtonStyle.success,
               custom_id="enzo_valider")
    async def valider(self, interaction: discord.Interaction, button: ui.Button):
        rows, idx = await self._ligne_du_jour()
        if idx is None:
            await interaction.response.send_message(
                "❌ Aucune séance trouvée pour aujourd'hui dans le planning.", ephemeral=True)
            return
        modale = CourseModal(idx) if est_course(cell(rows[idx], COL_TYPE)) else MuscuModal(idx)
        await interaction.response.send_modal(modale)

    @ui.button(label="❌ Non réalisée", style=discord.ButtonStyle.danger,
               custom_id="enzo_non_realise")
    async def non_realise(self, interaction: discord.Interaction, button: ui.Button):
        rows, idx = await self._ligne_du_jour()
        if idx is None:
            await interaction.response.send_message(
                "❌ Aucune séance trouvée pour aujourd'hui dans le planning.", ephemeral=True)
            return
        seance = cell(rows[idx], COL_SEANCE)
        report = await reporter_seance(idx, rows)
        marque = f"{seance} - ❌ Non réalisée"
        if report:
            marque += f", reportée au {report}"
        await asyncio.to_thread(write_cell, idx, COL_SEANCE, marque)
        if report:
            jour_fr = JOURS_FR[date.fromisoformat(report).strftime("%A")]
            suite = (f"Je l'ai reportée à **{jour_fr} {date.fromisoformat(report).strftime('%d/%m')}**, "
                     f"qui était un jour de repos. Ta semaine reste complète.")
        else:
            suite = ("Plus de jour de repos disponible cette semaine, je ne la reporte pas : "
                     "mieux vaut une séance en moins que deux le même jour.")
        await interaction.response.edit_message(
            content=f"<@{USER_ID}> C'est noté, pas de souci. 💪\n{suite}",
            view=None)
        await verifier_declencheur_coach(idx, interaction.channel, manquee=True)


# ── SEANCE MANQUEE : REPORT INTELLIGENT ───────────────────────────────────────
# Choix assume : on ne decale PAS tout le programme d'un jour. Cela deplacerait
# les sorties de course, casserait l'alternance haut / bas et ferait deraper les
# 12 semaines. On reporte la seance manquee sur le prochain jour de REPOS de la
# meme semaine, ce qui preserve le volume hebdomadaire et la structure du bloc.
# Si la semaine n'a plus de jour de repos disponible, la seance est perdue : on
# le dit franchement plutot que d'entasser deux seances sur un meme jour.

def _lundi(d_iso):
    d = date.fromisoformat(d_iso)
    return d - datetime.timedelta(days=d.weekday())

def chercher_report(rows, idx):
    """Renvoie l'index de la ligne d'accueil, ou None. On ne reporte que sur un
    jour de repos a venir, dans la meme semaine, et jamais sur un jour de course."""
    try:
        origine = _lundi(cell(rows[idx], COL_DATE))
    except ValueError:
        return None
    ce_jour = aujourdhui()
    for i in range(idx + 1, len(rows)):
        r = rows[i]
        d_txt = cell(r, COL_DATE)
        if not d_txt:
            continue
        try:
            d = date.fromisoformat(d_txt)
        except ValueError:
            continue
        if _lundi(d_txt) != origine:      # on ne deborde pas sur la semaine suivante
            return None
        if d <= ce_jour:
            continue
        if est_repos(cell(r, COL_SEANCE)) and cell(r, COL_TYPE).lower() == "repos":
            return i
    return None

async def reporter_seance(idx, rows=None):
    """Deplace la seance manquee. Renvoie la date d'accueil, ou None."""
    if rows is None:
        rows = await asyncio.to_thread(get_rows)
    seance = cell(rows[idx], COL_SEANCE)
    cible = chercher_report(rows, idx)
    if cible is None:
        return None
    await asyncio.to_thread(write_cell, cible, COL_SEANCE, seance)
    await asyncio.to_thread(write_cell, cible, COL_TYPE, cell(rows[idx], COL_TYPE))
    return cell(rows[cible], COL_DATE)

def seances_manquees_recentes(rows, idx, fenetre=7):
    """Nombre de seances marquees non realisees sur les derniers jours."""
    try:
        fin = date.fromisoformat(cell(rows[idx], COL_DATE))
    except ValueError:
        return 0
    debut = fin - datetime.timedelta(days=fenetre)
    n = 0
    for r in rows[1:]:
        d_txt = cell(r, COL_DATE)
        if not d_txt:
            continue
        try:
            d = date.fromisoformat(d_txt)
        except ValueError:
            continue
        if debut <= d <= fin and "Non réalisée" in cell(r, COL_SEANCE):
            n += 1
    return n


# ── COACH : DEPOT D'UNE DEMANDE POUR LA ROUTINE CLOUD ─────────────────────────
# Detection par MOTS ENTIERS, jamais par sous-chaine : « normal mais », « animal »
# et « mal dormi » declenchaient tous une fausse alerte douleur. Les parties du
# corps seules ne comptent pas non plus, « j'ai bossé le dos » est une seance
# normale : c'est la tournure « mal a/au » qui signale la plainte.
ALERTE = re.compile(
    r"\b(douleurs?|douloureu|blessure|bless[ée]|tendinite|tendinopathie|contracture|"
    r"claquage|entorse|[ée]longation|[ée]lancement|boite|boiter|g[êe]ne|g[êe]n[ée])\w*"
    # « mal » compte comme une plainte SAUF dans les tournures qui n'ont rien a
    # voir avec la douleur : mal dormi, mal mange, mal place, mal execute.
    r"|\bmal\b(?!\s*(?:dormi|dormir|mang|r[ée]veil|compris|plac|ex[ée]cut|fait|"
    r"couru|parti|g[ée]r[ée]|estim[ée]|calibr))", re.IGNORECASE)

def semaine_du_bloc(bloc_txt):
    """« Bloc 2 · S7 » -> 7. Renvoie None si illisible."""
    for morceau in bloc_txt.replace("·", " ").split():
        if morceau.upper().startswith("S") and morceau[1:].isdigit():
            return int(morceau[1:])
    return None

def raison_declencheur(row, rows=None, idx=None, manquee=False):
    """Renvoie la raison d'appeler le coach, ou None. Volontairement restrictif :
    Loys a demande une intervention sur les seances cles uniquement."""
    if ALERTE.search(cell(row, COL_RESSENTIS)):
        return "signal de douleur dans les ressentis"
    rpe = cell(row, COL_RPE).replace(",", ".")
    try:
        if float(rpe) >= 5:
            return "RPE 5 atteint, séance menée jusqu'à l'échec"
    except ValueError:
        pass
    semaine = semaine_du_bloc(cell(row, COL_BLOC))
    if semaine in (4, 8, 12) and cell(row, COL_JOUR).lower() == "dimanche":
        return f"fin du bloc (semaine {semaine})"
    if manquee and rows is not None and idx is not None:
        n = seances_manquees_recentes(rows, idx)
        if n >= 2:
            return f"{n} séances manquées en 7 jours"
    return None

def contexte_recent(rows, idx, nb=14):
    """Les dernieres lignes renseignees, pour que le coach ait de quoi juger."""
    lignes = []
    for r in rows[max(1, idx - nb):idx + 1]:
        if not r or not cell(r, COL_DATE):
            continue
        if not (cell(r, COL_CHARGES) or cell(r, COL_KM) or cell(r, COL_RESSENTIS)):
            continue
        lignes.append(" | ".join([
            cell(r, COL_DATE), cell(r, COL_BLOC), cell(r, COL_TYPE),
            "seance: " + cell(r, COL_SEANCE)[:90],
            "charges: " + (cell(r, COL_CHARGES) or "-"),
            "km: " + (cell(r, COL_KM) or "-"),
            "RPE: " + (cell(r, COL_RPE) or "-"),
            "ressentis: " + (cell(r, COL_RESSENTIS) or "-")]))
    return "\n".join(lignes)

def deposer_demande(texte):
    _svc().values().append(
        spreadsheetId=SPREADSHEET_ID, range=f"'{CMD_SHEET}'!A:E",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [[datetime.datetime.now(PARIS).strftime("%Y-%m-%d %H:%M"),
                          texte, "pending", "", ""]]}).execute()

async def verifier_declencheur_coach(row_idx, channel, manquee=False):
    """Appele apres chaque saisie. Depose une demande d'analyse si la seance le
    justifie. La routine cloud repondra au prochain passage."""
    try:
        rows = await asyncio.to_thread(get_rows)
        row = rows[row_idx]
        raison = raison_declencheur(row, rows, row_idx, manquee)
        if not raison:
            return
        demande = (
            f"COACH ENZO. Motif : {raison}. Analyse la seance du {cell(row, COL_DATE)} "
            f"et l'historique recent, puis poste un retour court et concret dans le salon "
            f"Discord d'Enzo (id {CHANNEL_ID}), en mentionnant <@{USER_ID}>. "
            f"Si une adaptation du planning te parait justifiee, NE MODIFIE PAS le planning : "
            f"depose-la dans l'onglet « {PROP_SHEET} » du classeur d'Enzo, "
            f"c'est Loys qui valide.\n\nHISTORIQUE :\n{contexte_recent(rows, row_idx)}")
        await asyncio.to_thread(deposer_demande, demande)
        await channel.send(
            f"🧠 J'ai demandé une analyse au coach ({raison}). "
            f"Sa réponse arrivera au prochain passage de la routine.")
    except Exception as e:
        print(f"⚠️ Enzo, declencheur coach : {e}")


# ── PROPOSITIONS DU COACH, VALIDEES PAR LOYS ──────────────────────────────────
def get_propositions():
    try:
        return _svc().values().get(
            spreadsheetId=SPREADSHEET_ID, range=f"'{PROP_SHEET}'!A:E").execute().get("values", [])
    except Exception:
        return []

def statut_proposition(idx, statut):
    _svc().values().update(
        spreadsheetId=SPREADSHEET_ID, range=f"'{PROP_SHEET}'!E{idx + 1}",
        valueInputOption="RAW", body={"values": [[statut]]}).execute()


class PropositionView(ui.View):
    """Adaptation proposee par le coach. Seul Loys peut trancher."""
    def __init__(self, idx=0, date_cible="", nouvelle=""):
        super().__init__(timeout=None)
        self.idx, self.date_cible, self.nouvelle = idx, date_cible, nouvelle
        for enfant in self.children:
            enfant.custom_id = f"{enfant.custom_id}_{idx}"

    async def _reserve_au_coach(self, interaction):
        if COACH_ID and interaction.user.id != COACH_ID:
            await interaction.response.send_message(
                "Seul le coach peut valider une adaptation du planning.", ephemeral=True)
            return False
        return True

    @ui.button(label="✅ Appliquer", style=discord.ButtonStyle.success,
               custom_id="enzo_prop_ok")
    async def appliquer(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._reserve_au_coach(interaction):
            return
        rows = await asyncio.to_thread(get_rows)
        cible = find_row(rows, self.date_cible)
        if cible is None:
            await interaction.response.send_message(
                f"❌ Aucune ligne au {self.date_cible} dans le planning.", ephemeral=True)
            return
        await asyncio.to_thread(write_cell, cible, COL_SEANCE, self.nouvelle)
        await asyncio.to_thread(statut_proposition, self.idx, "validée")
        await interaction.response.edit_message(
            content=(f"✅ Planning mis à jour pour le {self.date_cible} :\n"
                     f"> **{self.nouvelle}**"),
            view=None)

    @ui.button(label="❌ Refuser", style=discord.ButtonStyle.secondary,
               custom_id="enzo_prop_non")
    async def refuser(self, interaction: discord.Interaction, button: ui.Button):
        if not await self._reserve_au_coach(interaction):
            return
        await asyncio.to_thread(statut_proposition, self.idx, "refusée")
        await interaction.response.edit_message(
            content="❌ Proposition écartée, le planning reste inchangé.", view=None)


@tasks.loop(minutes=20)
async def propositions_task():
    """Poste les propositions du coach restees « en attente » dans le Sheet."""
    channel = _bot.get_channel(CHANNEL_ID) if _bot else None
    if not channel:
        return
    try:
        rows = await asyncio.to_thread(get_propositions)
    except Exception as e:
        print(f"⚠️ Enzo, lecture des propositions : {e}")
        return
    for i, r in enumerate(rows):
        if i == 0 or not r:
            continue
        date_cible = cell(r, 0)
        nouvelle   = cell(r, 1)
        motif      = cell(r, 2)
        statut     = cell(r, 4).lower()
        if statut != "en attente" or not (date_cible and nouvelle):
            continue
        # Le statut n'est ecrit qu'APRES un envoi reussi : sinon une proposition
        # refusee par Discord (message trop long, salon indisponible) resterait
        # marquee « postée » sans que personne ne l'ait jamais vue.
        contenu = (f"🧠 <@{COACH_ID}> Le coach propose d'adapter la séance "
                   f"du **{date_cible}** pour <@{USER_ID}>.\n\n"
                   f"**Nouvelle séance**\n{tableau_seance(nouvelle)}\n\n"
                   f"**Pourquoi**\n> {(motif or 'non précisé')[:600]}\n\n"
                   f"Tu valides ? 👇")
        try:
            await channel.send(content=contenu[:1990],
                               view=PropositionView(i, date_cible, nouvelle))
        except Exception as e:
            print(f"⚠️ Enzo, envoi de la proposition ligne {i + 1} : {e}")
            continue
        await asyncio.to_thread(statut_proposition, i, "postée")


# ── TACHES QUOTIDIENNES ───────────────────────────────────────────────────────
async def envoyer_seance(channel, rappel=True):
    """Poste la seance du jour d'Enzo. Partage par la tache de 8h00 et par la
    commande !seance tapee dans SON salon."""
    try:
        rows = await asyncio.to_thread(get_rows)
    except Exception as e:
        print(f"⚠️ Enzo, lecture du planning : {e}")
        if not rappel:
            await channel.send("❌ Impossible de lire le planning pour l'instant.")
        return
    today = aujourdhui()
    idx = find_row(rows, today.isoformat())
    if idx is None:
        if not rappel:
            await channel.send(
                f"📅 Aucune séance au programme aujourd'hui ({today.strftime('%d/%m/%Y')}). "
                f"Le programme court du 31/08 au 22/11/2026.")
        return                      # hors periode de programme, on ne dit rien
    row     = rows[idx]
    seance  = cell(row, COL_SEANCE)
    jour    = cell(row, COL_JOUR) or JOURS_FR[today.strftime("%A")]
    date_fr = today.strftime("%d/%m/%Y")
    bloc    = cell(row, COL_BLOC)

    if est_repos(seance):
        await channel.send(
            f"😴 <@{USER_ID}> Aujourd'hui c'est repos ({jour} {date_fr}).\n"
            f"La récupération fait partie du programme, profites-en. 🛋️")
        return
    entete = "💪 Hey <@%d> ! Ta séance du jour 👇" % USER_ID if rappel else "💪 Séance du jour"
    await channel.send(
        content=(f"{entete}\n\n"
                 f"**📅 {jour} {date_fr}**  ·  _{bloc}_\n"
                 f"{tableau_seance(seance)}\n\n"
                 f"Clique sur un bouton une fois la séance terminée."),
        view=SeanceView())


@tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=PARIS))
async def rappel_matin_task():
    channel = _bot.get_channel(CHANNEL_ID) if _bot else None
    if channel:
        await envoyer_seance(channel, rappel=True)


@tasks.loop(time=datetime.time(hour=21, minute=5, tzinfo=PARIS))
async def relance_soir_task():
    channel = _bot.get_channel(CHANNEL_ID) if _bot else None
    if not channel:
        return
    try:
        rows = await asyncio.to_thread(get_rows)
    except Exception as e:
        print(f"⚠️ Enzo, relance du soir : {e}"); return
    idx = find_row(rows, aujourdhui().isoformat())
    if idx is None:
        return
    row    = rows[idx]
    seance = cell(row, COL_SEANCE)
    if est_repos(seance) or "Non réalisée" in seance:
        return
    if cell(row, COL_CHARGES) or cell(row, COL_KM) or cell(row, COL_RESSENTIS):
        return                       # deja renseignee
    await channel.send(
        content=(f"⏰ <@{USER_ID}> Tu n'as pas encore renseigné ta séance du jour ! 👀\n"
                 f"{tableau_seance(seance)}\n\n"
                 f"Deux minutes et c'est réglé 👇"),
        view=SeanceView())


# ── COMMANDES TAPEES DANS LE SALON D'ENZO ────────────────────────────────────
# bot.py aiguille : une commande tapee dans #sport-enzo arrive ici et ne touche
# QUE le classeur d'Enzo. Une commande tapee dans le salon de Loys n'arrive
# jamais ici. Aucune des deux ne peut atteindre l'autre athlete.

_prochain_run = None      # fonction fournie par bot.py, pour annoncer l'heure


async def commande_semaine(channel):
    """!seance semaine : recap de la semaine d'Enzo, une ligne par jour."""
    rows = await asyncio.to_thread(get_rows)
    today = aujourdhui()
    lundi = today - datetime.timedelta(days=today.weekday())
    lignes = []
    for i in range(7):
        d = lundi + datetime.timedelta(days=i)
        idx = find_row(rows, d.isoformat())
        jour = JOURS_FR[d.strftime("%A")]
        marque = "👉 " if d == today else ""
        if idx is None:
            lignes.append(f"{marque}**{jour} {d.strftime('%d/%m')}** : _hors programme_")
            continue
        r = rows[idx]
        seance = cell(r, COL_SEANCE)
        fait = cell(r, COL_CHARGES) or cell(r, COL_KM)
        etat = "  ✅" if fait else ""
        lignes.append(f"{marque}**{jour} {d.strftime('%d/%m')}** : "
                      f"{resume_seance(seance) or '_rien de programmé_'}{etat}")
    await channel.send(f"📅 **Semaine du {lundi.strftime('%d/%m')} "
                       f"au {(lundi + datetime.timedelta(days=6)).strftime('%d/%m')}**\n\n"
                       + "\n".join(lignes))


async def commande_claude(channel, auteur_id, demande):
    """!claude dans le salon d'Enzo : depose la demande dans SON onglet Commandes."""
    if not demande.strip():
        await channel.send("Utilisation : `!claude <ta question ou ta demande>`")
        return
    try:
        rows = await asyncio.to_thread(get_rows)
        idx = find_row(rows, aujourdhui().isoformat())
        contexte = contexte_recent(rows, idx if idx is not None else len(rows) - 1)
        qui = "Loys (le coach)" if auteur_id == COACH_ID else "Enzo"
        texte = (f"COACH ENZO. Demande libre de {qui} depuis le salon Discord d'Enzo :\n"
                 f"{demande.strip()}\n\nHISTORIQUE :\n{contexte}")
        await asyncio.to_thread(deposer_demande, texte)
    except Exception as e:
        await channel.send(f"❌ Erreur : {e}")
        return
    if _prochain_run:
        run = int(_prochain_run().timestamp())
        await channel.send(f"✍️ Demande transmise au coach. Réponse au prochain "
                           f"passage : <t:{run}:t>, soit <t:{run}:R> ⏳")
    else:
        await channel.send("✍️ Demande transmise au coach.")


async def commande_programme(channel, auteur_id, date_raw, seance):
    """!programme dans le salon d'Enzo : reserve au coach, ecrit dans SON planning."""
    if COACH_ID and auteur_id != COACH_ID:
        await channel.send("Seul le coach peut modifier le planning d'Enzo.")
        return
    d_iso = None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m"):
        try:
            d = datetime.datetime.strptime(date_raw.strip(), fmt).date()
            if fmt == "%d/%m":
                d = d.replace(year=aujourdhui().year)
            d_iso = d.isoformat()
            break
        except ValueError:
            continue
    if not d_iso:
        await channel.send(f"Date `{date_raw}` non reconnue. Essaie `2026-09-14` ou `14/09`.")
        return
    rows = await asyncio.to_thread(get_rows)
    idx = find_row(rows, d_iso)
    if idx is None:
        await channel.send(f"Aucune ligne au {d_iso} dans le planning d'Enzo.")
        return
    await asyncio.to_thread(write_cell, idx, COL_SEANCE, seance)
    await channel.send(f"✅ Planning d'Enzo mis à jour pour le **{d_iso}** :\n"
                       f"{tableau_seance(seance)}")


# ── DEMARRAGE, APPELE PAR bot.py ──────────────────────────────────────────────
def reveiller_propositions():
    """Les boutons d'une proposition portent un custom_id indexe sur sa ligne, donc
    non reenregistrable au demarrage (discord.py 2.3). Une proposition deja postee
    mais sans reponse aurait des boutons morts : on la repasse en attente pour
    qu'elle soit repostee avec des boutons vivants."""
    try:
        for i, r in enumerate(get_propositions()):
            if i and r and cell(r, 4).lower() == "postée":
                statut_proposition(i, "en attente")
    except Exception as e:
        print(f"⚠️ Enzo, reveil des propositions : {e}")


def init(bot_instance, prochain_run=None):
    """Enregistre les vues persistantes et demarre les taches. Idempotent :
    on_ready est rejoue a chaque reconnexion Gateway."""
    global _bot, _prochain_run
    _prochain_run = prochain_run or _prochain_run
    if _bot is None:
        reveiller_propositions()
    _bot = bot_instance
    bot_instance.add_view(SeanceView())
    if not rappel_matin_task.is_running():  rappel_matin_task.start()
    if not relance_soir_task.is_running():  relance_soir_task.start()
    if not propositions_task.is_running():  propositions_task.start()
    print(f"✅ Suivi d'Enzo actif (salon {CHANNEL_ID})")


@rappel_matin_task.before_loop
@relance_soir_task.before_loop
@propositions_task.before_loop
async def _attendre_bot():
    if _bot:
        await _bot.wait_until_ready()
