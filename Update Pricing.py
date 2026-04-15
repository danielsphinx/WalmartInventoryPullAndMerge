import requests
import json
import pandas as pd
import xml.etree.ElementTree as ET
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')



def get_token():
    url = "https://marketplace.walmartapis.com/v3/token"
    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = requests.request("POST", url, headers=headers, data=payload)

    root = ET.fromstring(response.text)
    access_token_element = root.find(".//accessToken")

    access_token = access_token_element.text
    return access_token

def make_script_named(walmart_update_csv_path):
    access_token = get_token()

    url = "https://marketplace.walmartapis.com/v3/feeds?feedType=price"
    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json'
    }

    inventory_data = pd.read_csv(walmart_update_csv_path, encoding='utf-8-sig')

    # Set the batch size
    batch_size = 10000

    # Call the function to update prices in batches
    for i in range(0, len(inventory_data), batch_size):
        # Get a batch of rows
        batch = inventory_data.iloc[i:i + batch_size]

        # Call the function to process the current batch
        process_batch_prices(batch, url, headers)

def process_batch_prices(batch, url, headers):
    price_updates = []

    for index, row in batch.iterrows():
        sku = row['sku']
        price = row['price']

        price_update = {
            "sku": sku,
            "pricing": [
                {
                    "currentPrice": {
                        "currency": "USD",
                        "amount": price
                    }
                }
            ]
        }

        price_updates.append(price_update)

    payload = {
        "PriceHeader": {
            "version": "1.7"
        },
        "Price": price_updates
    }

    payload_json = json.dumps(payload)

    response = requests.post(url, headers=headers, data=payload_json)

    print("Update Response:", response.text)

# Example usage:
#  make_script_named(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\Update Merged.csv')
