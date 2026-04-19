from sqlalchemy import Column, Integer, Text
from geoalchemy2 import Geometry
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Polygon(Base):
    __tablename__ = "polygons"

    id = Column(Integer, primary_key=True)
    name = Column(Text)
    geom = Column(Geometry("MULTIPOLYGON", srid=4326))



import time
import requests
from seleniumwire import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from shapely.geometry import shape, MultiPolygon
from geoalchemy2.shape import from_shape

from models import Polygon  # adjust import if needed
from models import Base

# -------------------------
# DB SETUP
# -------------------------
from dotenv import load_dotenv
import os
load_dotenv()
DATABASE_URL = os.getenv("SUPABASE_DB_URL")

engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()

# -------------------------
# SELENIUM SETUP
# -------------------------
chrome_options = Options()
chrome_options.add_argument("--start-maximized")

driver = webdriver.Chrome(options=chrome_options)

# -------------------------
# START URL
# -------------------------
BASE_URL = "https://www.planning.data.gov.uk/entity/?dataset=article-4-direction-area"

driver.get(BASE_URL)
time.sleep(3)


def process_geojson(url):
    print(f"Fetching GeoJSON: {url}")

    response = requests.get(url)
    data = response.json()

    if data["type"] == "FeatureCollection":
        features = data["features"]
    else:
        features = [data]

    for feature in features:
        geom = feature.get("geometry")
        props = feature.get("properties", {})

        if not geom:
            continue

        shapely_geom = shape(geom)

        # Ensure MultiPolygon
        if shapely_geom.geom_type == "Polygon":
            shapely_geom = MultiPolygon([shapely_geom])

        name = props.get("name") or props.get("reference") or "unknown"

        poly = Polygon(
            name=name,
            geom=from_shape(shapely_geom, srid=4326)
        )

        session.add(poly)

    session.commit()
    print(f"Inserted {len(features)} features")


# -------------------------
# MAIN LOOP (pagination)
# -------------------------
while True:
    time.sleep(2)

    try:
        # Find GeoJSON link
        geojson_link = driver.find_element(
            By.XPATH, "//a[contains(@href, 'entity.geojson')]"
        )
        geojson_url = geojson_link.get_attribute("href")

        # Process data directly
        process_geojson(geojson_url)

    except Exception as e:
        print("GeoJSON link not found:", e)

    # Try clicking next page
    try:
        next_button = driver.find_element(
            By.XPATH, "//a[contains(@class, 'app-pagination__link') and contains(@rel, 'next')]"
        )

        next_url = next_button.get_attribute("href")
        print(f"Going to next page: {next_url}")

        driver.get(next_url)

    except Exception:
        print("No more pages.")
        break


driver.quit()
session.close()