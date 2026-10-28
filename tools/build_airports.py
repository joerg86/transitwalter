"""Erzeugt src/transitwalter/data/airports.csv aus OurAirports.

    curl -sLo /tmp/airports.csv https://davidmegginson.github.io/ourairports-data/airports.csv
    python3 tools/build_airports.py /tmp/airports.csv

OurAirports ist gemeinfrei. Übernommen werden alle Flughäfen mit IATA-Code und Linienverkehr.
"""

import csv
import sys
from pathlib import Path

OUT = Path(__file__).parent.parent / "src/transitwalter/data/airports.csv"
FIELDS = ["iata", "name", "city", "country", "lat", "lon"]


def main() -> None:
    rows = {}
    with open(sys.argv[1], encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["iata_code"] and r["scheduled_service"] == "yes" and r["type"] != "closed":
                rows[r["iata_code"]] = {
                    "iata": r["iata_code"], "name": r["name"], "city": r["municipality"],
                    "country": r["iso_country"], "lat": round(float(r["latitude_deg"]), 5),
                    "lon": round(float(r["longitude_deg"]), 5),
                }
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r["iata"]))
    print(f"{len(rows)} Flughäfen → {OUT}")


if __name__ == "__main__":
    main()
