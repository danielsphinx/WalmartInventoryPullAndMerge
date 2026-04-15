import requests
import json
import csv
import xml.etree.ElementTree as ET
from datetime import datetime
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

    response = requests.post(url, headers=headers, data=payload)
    if response.status_code == 200:
        root = ET.fromstring(response.text)
        access_token_element = root.find(".//accessToken")
        return access_token_element.text if access_token_element is not None else None
    else:
        print("Error getting token:", response.text)
        return None

def update_order_shipping(order_data):
    url_template = "https://marketplace.walmartapis.com/v3/orders/{purchaseOrderId}/shipping"

    access_token = gettoken()

    for row in order_data:
        api_url = url_template.replace("{purchaseOrderId}", row['purchaseOrderId'])

        # Get the current timestamp
        current_timestamp = int(datetime.now().timestamp()) * 1000  # Walmart API uses milliseconds

        payload = {
            "orderShipment": {
                "orderLines": {
                    "orderLine": [
                        {
                            "lineNumber": row['lineNumber'],
                            "intentToCancelOverride": "true",
                            "sellerOrderId": row['customerOrderId'],
                            "orderLineStatuses": {
                                "orderLineStatus": [
                                    {
                                        "status": "Shipped",
                                        "statusQuantity": {
                                            "unitOfMeasurement": "EACH",
                                            "amount": row['orderLineAmount']
                                        },
                                        "trackingInfo": {
                                            "shipDateTime": current_timestamp,
                                            "carrierName": {
                                                "carrier": row['carrier']
                                            },
                                            "methodCode": row['methodCode'],
                                            "trackingNumber": row['trackingNumber'],
                                            "trackingURL": row['trackingURL']
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }

        headers = {
            'Content-Type': 'application/json',
            'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
            'WM_SVC.NAME': 'My Walmart Inventory'
        }

        response = requests.post(api_url, headers=headers, json=payload)

        print(f"Processing purchaseOrderId {row['purchaseOrderId']}")
        print(f"API URL: {api_url}")
        print(f"Response: {response.status_code} - {response.text}")


csv_file_path = r'G:\Automation Google Drive\Google Sheet Connected CSVs\Order Tracking Generator AMS\Walmart Filter Pull AMS Tracking.csv'

def remove_bom_from_keys(row):
    """Remove the BOM from dictionary keys if present."""
    return {key.lstrip('\ufeff'): value for key, value in row.items()}

# Example usage with your CSV reading code
with open(csv_file_path, 'r', newline='', encoding='utf-8-sig') as file:
    reader = csv.DictReader(file)
    order_data = [remove_bom_from_keys(row) for row in reader]


update_order_shipping(order_data)
