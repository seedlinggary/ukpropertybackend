"""
Export all Article 4 polygons from the DB to a GeoJSON FeatureCollection.
Run: venv\Scripts\python.exe export_article4_geojson.py
"""
import json
from sqlalchemy import text
from database import engine

OUTPUT = "article4_polygons.geojson"
BATCH = 500

features = []
offset = 0
with engine.connect() as conn:
    while True:
        rows = conn.execute(text(
            "SELECT id, name, ST_AsGeoJSON(geom)::json AS geometry "
            "FROM polygons ORDER BY id LIMIT :lim OFFSET :off"
        ), {"lim": BATCH, "off": offset}).fetchall()
        if not rows:
            break
        for row in rows:
            features.append({
                "type": "Feature",
                "properties": {"id": row.id, "name": row.name},
                "geometry": row.geometry,
            })
        offset += BATCH
        print(f"  fetched {offset} / ~6854...")

fc = {"type": "FeatureCollection", "features": features}

with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(fc, f, ensure_ascii=False)

print(f"Exported {len(features)} polygons -> {OUTPUT}")
