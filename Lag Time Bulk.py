import requests
import csv
import json
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


def get_token():
    url = "https://marketplace.walmartapis.com/v3/token"
    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = walmart_post(url, kind="token", headers=headers, data=payload)  # feed-http
    return _http.token_from(response)  # feed-http

# Never relist gate first: a refusal must happen before any request leaves this process.
_retired = load_retired_skus()  # never-relist-gate

# Assuming you have the access_token already obtained
access_token = get_token()

url = "https://marketplace.walmartapis.com/v3/feeds?feedType=lagtime"

# Path to your CSV file containing SKUs
csv_file_path = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Merged Price And Quantity Walmart Export.csv'

# Initialize lagTime payload
lag_time_payload = {
    "lagTimeHeader": {
        "version": "1.1"
    },
    "lagTime": []
}

# Export check: the export is read once, as bytes; a copy cut off mid write (no final line break)
# or without the WalmartExportSku column is refused below, before anything is posted.
import io  # export-check
with open(csv_file_path, 'rb') as _export:  # export-check
    _export_bytes = _export.read()  # export-check
_export_whole = _export_bytes.endswith((b'\n', b'\r'))  # export-check

# Read SKUs from CSV and add to lagTime payload if availToSellQty is greater than 0
with io.StringIO(_export_bytes.decode('utf-8-sig'), newline='') as csvfile:  # export-check
    reader = csv.DictReader(csvfile)

    # Loop through each row in the CSV
    for row in reader:
        sku = row.get("WalmartExportSku", "").strip()  # Get SKU and strip any leading/trailing whitespace
        avail_qty = row.get("availToSellQty", "0").strip()  # Default to 0 if not present

        # Filter out rows where availToSellQty is 0 or invalid
        try:
            if sku and int(avail_qty) > 0:
                # If SKU exists and availToSellQty > 0, add it to the lagTime payload
                lag_time_payload["lagTime"].append({"sku": sku, "fulfillmentLagTime": 2})
        except ValueError:
            # Handle cases where availToSellQty cannot be converted to an integer
            print(f"Invalid availToSellQty for SKU '{sku}': '{avail_qty}', skipping this SKU.")

if not _export_whole or 'WalmartExportSku' not in (reader.fieldnames or []):  # export-check
    raise RuntimeError(f"Lag Time Bulk: {csv_file_path} " + ("does not end with a line break (cut off mid write?)" if not _export_whole else "has no WalmartExportSku column") + "; nothing was posted.")  # export-check

# Never relist gate: a lag time feed for a retired SKU re creates the listing.
_kept = set(drop_retired_skus([e['sku'] for e in lag_time_payload['lagTime']], _retired, 'lagtime Lag Time Bulk'))  # never-relist-gate
lag_time_payload['lagTime'] = [e for e in lag_time_payload['lagTime'] if e['sku'] in _kept]  # never-relist-gate

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
response = walmart_post(url, kind="feed", headers=headers, data=payload)  # feed-http

# Print the response
print(response.text)

