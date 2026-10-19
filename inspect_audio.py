import csv

with open("sound-items.csv", "r", encoding="utf-8-sig", newline="") as f:
  rows = list(csv.DictReader(f))

if not rows:
  print("No se han encontrado filas en el CSV.")
  raise SystemExit(1)

print("COLUMNAS:")
print(" | ".join(rows[0].keys()))
print("\nDISPOSITIVOS DE AUDIO:")

for row in rows:
  values = " | ".join(str(v or "") for v in row.values())
  if any(term in values.lower() for term in ("arctis", "realtek", "speakers", "altavoces", "microphone", "micrófono", "headset", "auriculares")):
    print(values)
