from sqlalchemy import select, func
from sqlalchemy.ext.declarative import declarative_base
import os
from sqlalchemy import Column, Integer, Text
import json
from sqlalchemy.orm import Session
import os
from sqlalchemy import create_engine
from geoalchemy2 import Geometry
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv
load_dotenv()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")
# print("DATABASE_URL:", DATABASE_URL)
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





session: Session = SessionLocal()


from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import JSON

session = SessionLocal()

try:
    results = session.execute(
        select(
            Polygon.id,
            Polygon.name,
            func.ST_AsGeoJSON(Polygon.geom).cast(JSON).label("geometry")
        )
    )

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": row.geometry,
                "properties": {
                    "id": row.id,
                    "name": row.name,
                },
            }
            for row in results
        ],
    }
    with open("polygons.geojson", "w") as f:
        json.dump(geojson, f)
finally:
    session.close()