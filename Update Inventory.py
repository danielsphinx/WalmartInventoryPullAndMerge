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


def update_inventory_batch(access_token, inventory_updates):
    url = "https://marketplace.walmartapis.com/v3/feeds?feedType=inventory"
    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json'
    }

    payload = {
        "InventoryHeader": {
            "version": "1.4"
        },
        "Inventory": inventory_updates
    }

    payload_json = json.dumps(payload)

    response = requests.post(url, headers=headers, data=payload_json)

    print(response.text)

def makescriptnamed(walmartupdatecsvpath):
    access_token = gettoken()

    # Read the entire inventory data from the CSV file
    inventory_data = pd.read_csv(walmartupdatecsvpath, encoding='utf-8-sig', low_memory=False, dtype={'ColumnName': str})

    # Set the batch size (e.g., 1000)
    batch_size = 10000

    for i in range(0, len(inventory_data), batch_size):
        # Get a batch of rows
        batch = inventory_data[i:i+batch_size]

        # Prepare inventory updates for this batch
        inventory_updates = []

        for index, row in batch.iterrows():
            sku = row['sku']
            quantity = row['quantity']

            inventory_update = {
                "sku": sku,
                "quantity": {
                    "unit": "EACH",
                    "amount": quantity
                }
            }

            inventory_updates.append(inventory_update)

        # Update inventory for this batch
        update_inventory_batch(access_token, inventory_updates)
# Example usage:
#makescriptnamed(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\Update Merged.csv')
#makescriptnamed(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\Delete Merged.csv')
#makescriptnamed(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\Update Merged New For Walmart Unmatched.csv')

