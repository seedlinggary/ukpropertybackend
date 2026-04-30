# import json

# files = ["file1.geojson", "file2.geojson", "file3.geojson"]
# all_features = []

# for i, file in enumerate(files):
#     with open(file) as f:
#         data = json.load(f)

#         for feature in data["features"]:
#             # Add a category so you know where it came from
#             feature["properties"]["source"] = f"dataset_{i+1}"
#             all_features.append(feature)

# merged = {
#     "type": "FeatureCollection",
#     "features": all_features
# }

# with open("merged.geojson", "w") as f:
#     json.dump(merged, f)


from sqlalchemy import Column, Integer, Text
from geoalchemy2 import Geometry
from sqlalchemy.ext.declarative import declarative_base
import os
import json
from shapely.geometry import shape, MultiPolygon
from sqlalchemy.orm import Session
from geoalchemy2.shape import from_shape
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
load_dotenv()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")
print("DATABASE_URL:", DATABASE_URL)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)

SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Polygon(Base):
    __tablename__ = "polygons"

    id = Column(Integer, primary_key=True)
    name = Column(Text)
    geom = Column(Geometry("MULTIPOLYGON", srid=4326))








def init_db():
    Base.metadata.create_all(bind=engine)
# init_db()
# from models import Polygon  # your model


def load_geojson_folder(folder_path):
    session: Session = SessionLocal()

    inserted = 0

    for filename in os.listdir(folder_path):
        if not filename.endswith(".geojson"):
            continue

        filepath = os.path.join(folder_path, filename)

        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)

        geometries = []

        # CASE 1: FeatureCollection (MOST COMMON)
        if data["type"] == "FeatureCollection":
            for feature in data["features"]:
                geometries.append(shape(feature["geometry"]))

        # CASE 2: Single Feature
        elif data["type"] == "Feature":
            geometries.append(shape(data["geometry"]))

        else:
            continue

        # Insert ALL geometries
        for geom in geometries:

            # Normalize
            if geom.geom_type == "Polygon":
                geom = MultiPolygon([geom])

            db_obj = Polygon(
                name=filename,
                geom=from_shape(geom, srid=4326)
            )

            session.add(db_obj)
            inserted += 1

    session.commit()
    session.close()

    print("INSERTED TOTAL:", inserted)

# load_geojson_folder(r'C:\Users\garys\OneDrive\Desktop\ukProperty website\backend')

51.566474161080336, -0.5546065309606113
from sqlalchemy import func

def check_point(lat, lng):
    session = SessionLocal()

    point = func.ST_SetSRID(func.ST_Point(lng, lat), 4326)

    result = session.query(Polygon).filter(
        func.ST_Contains(Polygon.geom, point)
    ).all()

    session.close()
    return result

# results = check_point(51.56758861769996, -0.55398864154942)  # London example
# points = [
#     (51.3000, 1.10),     # Canterbury large polygon
#     (51.5680, -0.6138),  # Farnham Common small polygon
#     (51.5645, -0.5575),  # Fulmer area
#     (51.5689, -0.5445),  # Gerrards Cross multipolygon
#     (51.5565, -0.5338),  # Iver area
# ]
# for item in points:
#     results = check_point(item[0],item[1])
#     if results:
#         for r in results:
#             print("Inside polygon:", r.id, r.name)
#     else:
#         print("Not inside any polygon")

# from sqlalchemy import func

# session = SessionLocal()

# centroids = session.query(
#     func.ST_Y(func.ST_Centroid(Polygon.geom)),
#     func.ST_X(func.ST_Centroid(Polygon.geom))
# ).limit(10).all()

# print(centroids)