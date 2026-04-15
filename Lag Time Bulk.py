import requests
import csv
import json
import xml.etree.ElementTree as ET
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')
WM_QOS_CORRELATION_ID = os.getenv('WM_QOS_CORRELATION_ID')


def get_token():
    url = "https://marketplace.walmartapis.com/v3/token"
    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = requests.post(url, headers=headers, data=payload)
    if response.status_code == 200:
        root = ET.fromstring(response.text)
        access_token_element = root.find(".//accessToken")
        return access_token_element.text if access_token_element is not None else None
    else:
        print("Error getting token:", response.text)
        return None

# Assuming you have the access_token already obtained
access_token = get_token()

url = "https://marketplace.walmartapis.com/v3/feeds?feedType=lagtime"

# Path to your CSV file containing SKUs
csv_file_path = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Merged Price And Quantity Walmart Export.csv'

# Initialize lagTime payload
lag_time_payload = {
    "LagTimeHeader": {
        "version": "1.0"
    },
    "lagTime": []
}

# Read SKUs from CSV and add to lagTime payload if availToSellQty is greater than 0
with open(csv_file_path, 'r', newline='', encoding='utf-8') as csvfile:
    reader = csv.DictReader(csvfile)

    # Loop through each row in the CSV
    for row in reader:
        sku = row.get("WalmartExportSku", "").strip()  # Get SKU and strip any leading/trailing whitespace
        avail_qty = row.get("availToSellQty", "0").strip()  # Default to 0 if not present

        # Filter out rows where availToSellQty is 0 or invalid
        try:
            if sku and int(avail_qty) > 0:
                # If SKU exists and availToSellQty > 0, add it to the lagTime payload
                lag_time_payload["lagTime"].append({"sku": sku, "fulfillmentLagTime": "1"})
        except ValueError:
            # Handle cases where availToSellQty cannot be converted to an integer
            print(f"Invalid availToSellQty for SKU '{sku}': '{avail_qty}', skipping this SKU.")

# Convert payload to JSON string
payload = json.dumps(lag_time_payload)
#  print(payload)

headers = {
    'WM_SEC.ACCESS_TOKEN': access_token,
    'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
    'WM_SVC.NAME': 'Walmart Marketplace',
    'Content-Type': 'application/json'
}

# Make the POST request
response = requests.post(url, headers=headers, data=payload)

# Print the response
print(response.text)
