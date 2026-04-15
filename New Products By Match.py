import requests
import json
import pandas as pd
import xml.etree.ElementTree as ET
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')
WM_QOS_CORRELATION_ID = os.getenv('WM_QOS_CORRELATION_ID')

def gettoken():
    url = "https://marketplace.walmartapis.com/v3/token"
    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = requests.request("POST", url, headers=headers, data=payload)
    print(response.text)

    root = ET.fromstring(response.text)
    access_token_element = root.find(".//accessToken")
    access_token = access_token_element.text
    return access_token

def upload_new_products(access_token, new_product_uploads):
    url = "https://marketplace.walmartapis.com/v3/feeds?feedType=MP_ITEM_MATCH"
    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json'
    }

    payload = {
        "MPItemFeedHeader": {
            "processMode": "REPLACE",
            "subset": "EXTERNAL",
            "locale": "en",
            "sellingChannel": "mpsetupbymatch",
            "version": "4.2"
        },
        "MPItem": new_product_uploads
    }

    payload_json = json.dumps(payload)
    response = requests.post(url, headers=headers, data=payload_json)
    print(response.text)

def make_new_product_uploads(new_product_csv_path):
    access_token = gettoken()

    # Read new product data from the CSV file
    new_product_data = pd.read_csv(new_product_csv_path)
    print(new_product_data)

    # Set the batch size (e.g., 1000)
    batch_size = 1000

    for i in range(0, len(new_product_data), batch_size):
        # Get a batch of rows
        batch = new_product_data[i:i+batch_size]

        # Prepare new product uploads for this batch
        new_product_uploads = []

        for index, row in batch.iterrows():
            sku = row['sku']
            product_id = str(row['productId']).rjust(14, '0')
            weight = row['ShippingWeight']
            price = row['price']

            new_product_upload = {
                "Item": {
                    "sku": sku,
                    "condition": "New",
                    "productIdentifiers": {
                        "productIdType": "GTIN",
                        "productId": str(product_id)  # Convert product_id to a string
                    },
                    "ShippingWeight": weight,
                    "price": price
                }
            }

            new_product_uploads.append(new_product_upload)
        print("MPItem: ", new_product_uploads)

        # Upload new products for this batch
        upload_new_products(access_token, new_product_uploads)

# Example usage
#  make_new_product_uploads(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\New Merged Existing For Match.csv')
