from flask import Flask, jsonify, request
from db import supabase

app = Flask(__name__)

TABLE_NAME = "properties"


# ✅ Get single property
@app.route("/property/<int:property_id>", methods=["GET"])
def get_property(property_id):
    try:
        response = supabase.table(TABLE_NAME)\
            .select("*")\
            .eq("id", property_id)\
            .execute()

        data = response.data

        if not data:
            return jsonify({"error": "Property not found"}), 404

        return jsonify(data[0]), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ✅ Get all properties (with optional filters)
@app.route("/properties", methods=["GET"])
def get_properties():
    try:
        query = supabase.table(TABLE_NAME).select("*")

        # Optional filters
        postcode = request.args.get("postcode")
        min_price = request.args.get("min_price")
        max_price = request.args.get("max_price")

        if postcode:
            query = query.eq("postcode", postcode)

        if min_price:
            query = query.gte("price", min_price)

        if max_price:
            query = query.lte("price", max_price)

        response = query.execute()

        return jsonify(response.data), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)