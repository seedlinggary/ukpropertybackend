# from flask import Flask, jsonify, request
# # from db import supabase
# from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, Text
# from sqlalchemy.orm import sessionmaker, declarative_base
# from datetime import datetime, timedelta
# import requests
# from geoalchemy2 import Geometry
# import json
# import os
# from dotenv import load_dotenv
# load_dotenv()
# DATABASE_URL = os.getenv("SUPABASE_DB_URL")
# print("DATABASE_URL:", DATABASE_URL)
# engine = create_engine(DATABASE_URL, pool_pre_ping=True)
# SessionLocal = sessionmaker(bind=engine)

# Base = declarative_base()


# apikey = "ENTERYOURAPIKEYHERE"
# postcode = "SE6 1PH"
# searchtype = "all"

# url = "https://api.article4map.com/text"
# headers = {'Authorization': apikey, 'Content-type': "application/json"}
# data = {"search": postcode, "type" : searchtype}
# ‍
# result = requests.post(url, headers=headers, data=json.dumps(data))
# ‍
# result.raise_for_status()
# print(result.content)
# class Property(Base):
#     __tablename__ = "properties"

#     id = Column(Integer, primary_key=True, index=True)
#     postcode = Column(String(20), unique=True, nullable=False, index=True)
#     is_article4 = Column(Boolean, nullable=False)
#     last_updated = Column(DateTime, nullable=False, default=datetime.utcnow)

# class Polygon(Base):
#     __tablename__ = "polygons"

#     id = Column(Integer, primary_key=True)
#     name = Column(Text)
#     geom = Column(Geometry("MULTIPOLYGON", srid=4326))
# def init_db():
#     Base.metadata.create_all(bind=engine)

# def fetch_article4_from_api(postcode: str) -> bool | None:
#     """
#     Replace with real API
#     """
#     try:
#         url = f"https://example.com/api/article4?postcode={postcode}"
#         response = requests.get(url, timeout=5)

#         if response.status_code == 200:
#             data = response.json()
#             return data.get("is_article4", False)

#         return None

#     except Exception as e:
#         print(f"API error: {e}")
#         return None



# def get_article4_status(postcode: str, max_age_days: int = 30):
#     postcode = postcode.strip().upper()
#     db = SessionLocal()

#     try:
#         # 1. CHECK DB
#         record = db.query(Property).filter_by(postcode=postcode).first()

#         if record:
#             if datetime.utcnow() - record.last_updated < timedelta(days=max_age_days):
#                 return {
#                     "source": "database",
#                     "postcode": postcode,
#                     "is_article4": record.is_article4,
#                     "last_updated": record.last_updated.isoformat()
#                 }

#         # 2. FETCH FROM API
#         is_article4 = fetch_article4_from_api(postcode)

#         if is_article4 is None:
#             return {"error": "External API failed"}

#         # 3. UPSERT (ORM STYLE)
#         if record:
#             record.is_article4 = is_article4
#             record.last_updated = datetime.utcnow()
#         else:
#             record = Property(
#                 postcode=postcode,
#                 is_article4=is_article4,
#                 last_updated=datetime.utcnow()
#             )
#             db.add(record)

#         db.commit()

#         return {
#             "source": "external_api",
#             "postcode": postcode,
#             "is_article4": is_article4,
#             "last_updated": record.last_updated.isoformat()
#         }

#     finally:
#         db.close()


# def get_article4_bulk(postcodes: list[str]):
#     return [get_article4_status(pc) for pc in postcodes]
# # # ✅ Get single property
# @app.route("/property/<int:property_id>", methods=["GET"])
# def get_property(property_id):
#     try:
#         response = supabase.table(TABLE_NAME)\
#             .select("*")\
#             .eq("id", property_id)\
#             .execute()

#         data = response.data

#         if not data:
#             return jsonify({"error": "Property not found"}), 404

#         return jsonify(data[0]), 200

#     except Exception as e:
#         return jsonify({"error": str(e)}), 500


# # # ✅ Get all properties (with optional filters)
# # @app.route("/properties", methods=["GET"])
# # def get_properties():
# #     try:
# #         query = supabase.table(TABLE_NAME).select("*")

# #         # Optional filters
# #         postcode = request.args.get("postcode")
# #         min_price = request.args.get("min_price")
# #         max_price = request.args.get("max_price")

# #         if postcode:
# #             query = query.eq("postcode", postcode)

# #         if min_price:
# #             query = query.gte("price", min_price)

# #         if max_price:
# #             query = query.lte("price", max_price)

# #         response = query.execute()

# #         return jsonify(response.data), 200

# #     except Exception as e:
# #         return jsonify({"error": str(e)}), 500

import logging

from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import text

from geoutils import check_point
from database import engine
from models import Base  # imports PropertyListing + ScraperRun into Base metadata
from routes.scraper import scraper_bp
from routes.properties import properties_bp
from scheduler import start_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

app = Flask(__name__)
CORS(app)

# Create new DB tables (property_listings, scraper_runs) if they don't exist yet.
# The existing polygons table (owned by geoutils.py's Base) is unaffected.
Base.metadata.create_all(bind=engine)

app.register_blueprint(scraper_bp)
app.register_blueprint(properties_bp)

# Start the 12-hour background scheduler.
# Called here (module level) so it runs under gunicorn too, not just `python app.py`.
# start_scheduler() is idempotent — safe to call multiple times.
start_scheduler()


# --- existing routes (unchanged) ---

@app.route("/", methods=["GET"])
def home():
    return "hello world"


@app.route("/location", methods=["POST"])
def location():
    data = request.get_json()

    lat = data.get("lat")
    lng = data.get("lng")

    if lat is None or lng is None:
        return jsonify({"error": "Missing lat or lng"}), 400

    article4 = check_point(lat, lng)
    is_article4 = bool(article4)

    return jsonify({
        "lat": lat,
        "lng": lng,
        "message": "Coordinates received",
        "Article_4": is_article4,
    })


@app.route("/admin/backfill-article4", methods=["POST"])
def backfill_article4():
    """
    One-shot route: recomputes article4 for every property that has lat/lng
    using a single PostGIS UPDATE — much faster than row-by-row Python.
    POST /admin/backfill-article4
    """
    try:
        with engine.connect() as conn:
            geo = conn.execute(text("""
                UPDATE property_listings
                SET article4 = (
                    SELECT COUNT(*) > 0
                    FROM polygons
                    WHERE ST_Contains(
                        polygons.geom,
                        ST_SetSRID(ST_Point(property_listings.lng, property_listings.lat), 4326)
                    )
                )
                WHERE lat IS NOT NULL AND lng IS NOT NULL
            """))
            nulled = conn.execute(text("""
                UPDATE property_listings
                SET article4 = NULL
                WHERE lat IS NULL OR lng IS NULL
            """))
            conn.commit()
        return jsonify({"status": "ok", "geo_updated": geo.rowcount, "nulled": nulled.rowcount})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(debug=True)
# if __name__ == "__main__":
#     init_db()

#     result = get_article4_status("SW1A 1AA")
#     print(result) 