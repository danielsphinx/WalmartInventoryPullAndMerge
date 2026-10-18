import requests
import json
import pandas as pd
import xml.etree.ElementTree as ET
import os
import importlib.util  # never-relist-gate
_gate_spec = importlib.util.spec_from_file_location("walmart_retired_filter", r"G:\Python Projects\WalmartInventoryPullAndMerge\walmart_retired_filter.py")  # never-relist-gate
_gate = importlib.util.module_from_spec(_gate_spec)  # never-relist-gate
_gate_spec.loader.exec_module(_gate)  # never-relist-gate
load_retired_skus, drop_retired_rows, drop_retired_skus = _gate.load_retired_skus, _gate.drop_retired_rows, _gate.drop_retired_skus  # never-relist-gate
_http_spec = importlib.util.spec_from_file_location("walmart_feed_http", r"G:\Python Projects\WalmartInventoryPullAndMerge\walmart_feed_http.py")  # feed-http
_http = importlib.util.module_from_spec(_http_spec)  # feed-http
_http_spec.loader.exec_module(_http)  # feed-http
walmart_post = _http.post  # feed-http

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

    response = walmart_post(url, kind="token", headers=headers, data=payload)  # feed-http

    print(response.text)

    return _http.token_from(response)  # feed-http


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

    response = walmart_post(url, kind="feed", headers=headers, data=payload_json)  # feed-http

    print(response.text)

@_http.logged("Update Inventory")  # feed-http
def makescriptnamed(walmartupdatecsvpath):
    # Never relist gate first: a refusal must happen before any request leaves this process.
    _retired = load_retired_skus()  # never-relist-gate
    access_token = gettoken()

    # Read the entire inventory data from the CSV file
    inventory_data = pd.read_csv(walmartupdatecsvpath, encoding='utf-8-sig', low_memory=False, dtype={'ColumnName': str})
    inventory_data = drop_retired_rows(inventory_data, 'sku', 'inventory Update Inventory', _retired)  # never-relist-gate

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

