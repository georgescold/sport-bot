"""apply_cmd.py — applique un plan JSON (stdin) sur le sheet + Discord.

Utilisé par la routine Claude qui traite les demandes !claude.
Format du plan :
{"command_row": <int>, "edits": [{"date": "YYYY-MM-DD", "col": <int>, "value": "..."}],
 "discord_message": "...", "result": "..."}
"""
import sys, json
import sport_bot as sb

plan = json.load(sys.stdin)
svc, rows, applied = sb.get_sheets_service(), sb.get_all_rows(), []

# Colonnes protégées en écriture : date(0/A), jour(1/B), 9, + « Notes Nico »
# (colonne du COACH) résolue par en-tête pour rester correcte après tout décalage.
protected = {0, 1, 9}
for i, name in enumerate(rows[0] if rows else []):
    n = (name or "").strip().lower()
    if "notes" in n and "nico" in n:
        protected.add(i)

for e in plan.get("edits", []):
    idx = sb.find_row_for_date(rows, e["date"])
    if idx is None:
        applied.append("date " + e["date"] + " introuvable"); continue
    col = int(e["col"])
    if col in protected:
        applied.append("colonne " + str(col) + " protegee"); continue
    sb.update_cell(svc, idx, col, str(e["value"]))
    applied.append(chr(65 + col) + "@" + e["date"] + "=" + str(e["value"]))
msg = (plan.get("discord_message") or "").strip()
if msg:
    sb.send_message(msg)
sb.mark_command_done(int(plan["command_row"]), plan.get("result") or "; ".join(applied))
print(json.dumps({"applied": applied}, ensure_ascii=False))
