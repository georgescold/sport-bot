"""migrate_km.py — rapatrie les km quotidiens mal placés vers « km_journee ».

Contexte : avant l'insertion de « Notes Nico » (15/06/2026), le bot écrivait les
km du jour dans la colonne devenue « km_semaine ». Ce script recopie ces valeurs
vers « km_journee » (et vide la source).

  python migrate_km.py            # DRY-RUN : affiche ce qui SERAIT déplacé, n'écrit rien
  python migrate_km.py --apply    # applique réellement (copie vers km_journee + vide km_semaine)
  python migrate_km.py --apply --keep-source   # copie sans vider km_semaine

Sécurités :
- DRY-RUN par défaut → aucune écriture sans --apply (relis la sortie d'abord).
- ne déplace QUE les lignes où km_journee est VIDE et km_semaine contient un nombre.
- ne touche jamais « Notes Nico » ni les autres colonnes.
- ⚠️  Si « km_semaine » contient des totaux/formules hebdo légitimes, NE PAS lancer
      --apply : vérifie d'abord la liste affichée en dry-run.

À exécuter côté cloud (session Claude Code web : le hook SessionStart recrée les
credentials Google). En local, le fichier de credentials est absent.
"""
import argparse
import sys
import sport_bot as sb


def col_by_header(header, *needles, exclude=()):
    """Index 0-based de la 1re colonne dont l'en-tête contient tous les `needles`
    et aucun mot de `exclude`. None si introuvable."""
    for i, raw in enumerate(header):
        n = (raw or "").strip().lower()
        if all(k in n for k in needles) and not any(x in n for x in exclude):
            return i
    return None


def is_number(s):
    try:
        float(s.strip().replace(",", "."))
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="écrit réellement (sinon dry-run)")
    ap.add_argument("--keep-source", action="store_true", help="ne vide pas km_semaine")
    args = ap.parse_args()

    rows = sb.get_all_rows()
    if not rows:
        print("Feuille vide — rien à faire.")
        return
    header = rows[0]

    jc = col_by_header(header, "km", "jour", exclude=("semaine",))
    sc = col_by_header(header, "km", "semaine")
    lj = chr(65 + jc) if jc is not None else "?"
    ls = chr(65 + sc) if sc is not None else "?"
    print(f"En-têtes détectés : km_journee = {lj} (idx {jc}) | km_semaine = {ls} (idx {sc})")

    if jc is None or sc is None or jc == sc:
        print("❌ Colonnes km_journee/km_semaine introuvables ou ambiguës par en-tête. Abandon.")
        sys.exit(1)

    moves = []
    for i, row in enumerate(rows):
        if i == 0 or not row or not row[0].strip():
            continue  # en-tête / lignes vides
        jv = row[jc].strip() if len(row) > jc else ""
        sv = row[sc].strip() if len(row) > sc else ""
        if jv == "" and sv != "" and is_number(sv):
            moves.append((i, row[0].strip(), sv))

    if not moves:
        print("Aucune valeur à déplacer (km_journee déjà rempli, ou km_semaine non numérique/vide).")
        return

    print(f"\n{len(moves)} valeur(s) candidate(s) :")
    for i, d, v in moves:
        print(f"  ligne {i + 1} | {d} | {v} km : {ls} -> {lj}")

    if not args.apply:
        print("\n⚠️  Vérifie que km_semaine ne contient pas de totaux/formules hebdo légitimes.")
        print("DRY-RUN — rien écrit. Relance avec --apply pour appliquer.")
        return

    svc = sb.get_sheets_service()
    for i, d, v in moves:
        sb.update_cell(svc, i, jc, v)            # copie vers km_journee
        if not args.keep_source:
            sb.update_cell(svc, i, sc, "")       # vide km_semaine
    suffix = "" if args.keep_source else " (source vidée)"
    print(f"\n✅ {len(moves)} valeur(s) déplacée(s){suffix}.")


if __name__ == "__main__":
    main()
