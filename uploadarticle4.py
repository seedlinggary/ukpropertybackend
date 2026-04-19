import requests
import os
from dotenv import load_dotenv

from sqlalchemy import create_engine, Column, Integer, Text
from sqlalchemy.orm import sessionmaker, declarative_base

from geoalchemy2 import Geometry
from geoalchemy2.shape import from_shape
from shapely.geometry import shape, MultiPolygon

# -------------------------
# CONFIG
# -------------------------
load_dotenv()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")

BASE_URL = "https://www.planning.data.gov.uk/entity.geojson"
DATASET = "article-4-direction-area"
LIMIT = 10

# -------------------------
# DB SETUP
# -------------------------
Base = declarative_base()

class Polygon(Base):
    __tablename__ = "polygons"

    id = Column(Integer, primary_key=True)
    name = Column(Text)
    geom = Column(Geometry("MULTIPOLYGON", srid=4326))


engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

Base.metadata.create_all(engine)

# -------------------------
# FETCH DATA
# -------------------------
def fetch_page(offset):
    url = f"{BASE_URL}?dataset={DATASET}&limit={LIMIT}&offset={offset}"
    print(f"Fetching offset {offset}")
    r = requests.get(url)
    r.raise_for_status()
    return r.json()

# -------------------------
# CONVERT FEATURE → ORM OBJECT
# -------------------------
from shapely.geometry import shape, MultiPolygon, Polygon as ShapelyPolygon
total_skipped = 0
def convert_feature(feature):
    global total_skipped  # 👈 required
    geom = feature.get("geometry")
    props = feature.get("properties", {})

    if not geom:
        total_skipped += 1
        print("SKIPPED: missing geometry")
        return None


    shapely_geom = shape(geom)

    # ✅ ONLY accept polygon types
    if shapely_geom.geom_type == "Polygon":
        shapely_geom = MultiPolygon([shapely_geom])

    elif shapely_geom.geom_type == "MultiPolygon":
        pass

    else:
        total_skipped+=1

        print(f"SKIPPED:{total_skipped} - ", shapely_geom.geom_type, props.get("name"))
        return None

    return Polygon(
        name=props.get("name")
            or props.get("reference")
            or props.get("id")
            or "unknown",
        geom=from_shape(shapely_geom, srid=4326)
    )

# -------------------------
# MAIN INGESTION LOOP
# -------------------------
def run():
    global total_skipped
    offset = 0
    total = 0

    session = SessionLocal()

    try:
        while True:
            data = fetch_page(offset)
            features = data.get("features", [])

            if not features:
                print("Done.")
                break

            batch = []

            for feature in features:
                poly = convert_feature(feature)
                if poly:
                    batch.append(poly)
                else:
                    print('poly', poly)

            # session.add_all(batch)
            # session.commit()

            total += len(batch)
            print(f"Inserted {len(batch)} | Total {total}")

            offset += LIMIT

    except Exception as e:
        session.rollback()
        print("Error:", e)

    finally:
        session.close()
        print("FINAL TOTAL:", total)
        print("total_skipped:", total_skipped)

# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    run()