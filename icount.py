import requests

API_URL = "https://api.icount.co.il/api/v3.php"

def create_invoice():
    payload = {
        "cid": 'garystar',
        "user": "gary.s.schwartz617@gmail.com",
        "pass": "Garystar617",

        # API method
        "doctype": "invrec",  # invoice + receipt
        "path": "/doc/create",
        # Client info
        "client_name": "John Doe",
        "email": "john@example.com",

        # Item details
        "items": [
            {
                "description": "Therapy Session",
                "unitprice": 300,
                "quantity": 1
            }
        ],

        # Optional
        "lang": "en",
        "currency_code": "ILS"
    }

    try:
        response = requests.post(API_URL, json=payload, timeout=10)
        response.raise_for_status()

        data = response.json()

        if data.get("status") != "ok":
            print("API error:", data)
            return

        print("Invoice created successfully!")
        print("Doc ID:", data.get("doc_id"))
        print("PDF URL:", data.get("doc_url"))

    except requests.exceptions.RequestException as e:
        print("Request failed:", str(e))


if __name__ == "__main__":
    create_invoice()