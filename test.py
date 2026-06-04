import requests
import xml.etree.ElementTree as ET
import csv
import json
import pandas as pd
import time
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')
WM_QOS_CORRELATION_ID = os.getenv('WM_QOS_CORRELATION_ID')

# Path to the merged inventory output (used both as fallback source and final output destination)
MERGED_OUTPUT_PATH = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Merged Price And Quantity Walmart Export.csv'
QTY_OUTPUT_PATH = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Quantity Export.csv'
PRICES_OUTPUT_PATH = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Prices Export.csv'

# Fallback quantity assigned to SKUs that were in the previous file but missing from the new scrape.
# This is intentional: a non-zero value forces Walmart's update process to touch these SKUs
# instead of silently skipping them when they're absent from the new export.
FALLBACK_QTY = 10


def load_previous_inventory(path):
    """
    Load the previous merged inventory CSV and return a dict of {sku: quantity}.
    Returns an empty dict if the file does not exist or cannot be read.
    """
    if not os.path.exists(path):
        print(f"[WARNING] Previous inventory file not found at: {path}")
        print("[WARNING] Continuing with only the current scrape. No fallback data will be used.")
        return {}

    try:
        df = pd.read_csv(path, dtype=str)

        # The merged file uses 'WalmartExportSku' and 'availToSellQty'
        if 'WalmartExportSku' not in df.columns or 'availToSellQty' not in df.columns:
            print(f"[WARNING] Previous inventory file is missing expected columns. Skipping fallback.")
            return {}

        prev = {}
        for _, row in df.iterrows():
            sku = str(row['WalmartExportSku']).strip()
            qty = str(row['availToSellQty']).strip()
            if sku:
                prev[sku] = qty

        print(f"[INFO] Loaded {len(prev)} SKUs from previous inventory file.")
        return prev

    except Exception as e:
        print(f"[WARNING] Could not read previous inventory file: {e}")
        print("[WARNING] Continuing with only the current scrape.")
        return {}


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

    root = ET.fromstring(response.text)
    access_token_element = root.find(".//accessToken")
    access_token = access_token_element.text
    return access_token


def getInventory():
    products_retrieved = 0
    sku_quantity_data = []
    merged_inventory_data = []
    next_cursor = None
    max_retries = 3

    while True:
        url = "https://marketplace.walmartapis.com/v3/inventories?limit=50"

        if next_cursor is not None:
            url += f"&nextCursor={next_cursor}"

        retries = 0
        success = False

        while retries < max_retries and not success:
            access_token = gettoken()

            headers = {
                'WM_SEC.ACCESS_TOKEN': access_token,
                'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
                'WM_SVC.NAME': 'YOUR_SERVICE_NAME',
                'Accept': 'application/json'
            }

            response = requests.get(url, headers=headers)
            inventory_info = response.json()

            if 'error' in inventory_info:
                retries += 1
                print(f"Error fetching inventory (attempt {retries}/{max_retries}):", inventory_info)
                time.sleep(10)
            else:
                success = True
                products_retrieved += 50
                print(f"{products_retrieved} products qty checked")

        if not success:
            print("Failed to fetch inventory data after maximum retries. Continuing to the next page if available.")
            continue

        if 'elements' not in inventory_info or 'inventories' not in inventory_info['elements']:
            print("Unexpected response format:", inventory_info)
            continue

        merged_inventory_data.extend(inventory_info['elements']['inventories'])

        if 'nextCursor' in inventory_info.get('meta', {}):
            next_cursor = inventory_info['meta']['nextCursor']
        else:
            break

    for inventory_item in merged_inventory_data:
        sku = inventory_item.get('sku', "")
        avail_to_sell_qty = 0

        if 'nodes' in inventory_item:
            for node in inventory_item['nodes']:
                avail_to_sell_qty = node['availToSellQty'].get('amount', 0) if 'availToSellQty' in node else 0

        sku_quantity_data.append([sku, avail_to_sell_qty])

    return sku_quantity_data


def apply_fallback(sku_quantity_data, previous_inventory):
    """
    Compare the freshly scraped SKUs against the previous inventory file.
    Any SKU that was in the previous file but is missing from the new scrape gets added
    back with FALLBACK_QTY. This prevents incomplete scrapes from leaving old Walmart
    inventory untouched.

    Nothing is ever marked in stock based solely on the previous file.
    The fallback quantity (10) is a deliberate signal, not a real stock count.
    """
    scraped_skus = {str(row[0]).strip() for row in sku_quantity_data if row[0]}
    scraped_count = len(scraped_skus)

    fallback_rows = []
    missing_skus = []

    for sku, qty in previous_inventory.items():
        if sku not in scraped_skus:
            missing_skus.append(sku)
            fallback_rows.append([sku, FALLBACK_QTY])

    # Build final list: all scraped rows + fallback rows for missing SKUs
    final_data = list(sku_quantity_data) + fallback_rows

    # Logging
    print(f"\n{'='*60}")
    print(f"[SCRAPE SUMMARY]")
    print(f"  SKUs found in this scrape:          {scraped_count}")
    print(f"  SKUs in previous inventory file:    {len(previous_inventory)}")
    print(f"  SKUs missing from new scrape:       {len(missing_skus)}")
    print(f"  Total SKUs in final output:         {len(final_data)}")
    if missing_skus:
        print(f"\n[FALLBACK SKUs] (added with qty={FALLBACK_QTY} because they were missing from this scrape):")
        for s in sorted(missing_skus):
            print(f"    {s}")
    else:
        print(f"\n[FALLBACK SKUs] None. All previous SKUs were found in this scrape.")
    print(f"{'='*60}\n")

    return final_data


def getPricing():
    sku_amount_data = []
    merged_root = ET.Element('ItemResponses', {'xmlns:wmSpecs': 'http://walmart.com'})
    merged_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ItemResponses xmlns:wmSpecs="http://walmart.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ns2="http://walmart.com"/>'

    next_cursor = '*'

    while True:
        url = f"https://marketplace.walmartapis.com/v3/items?limit=200&nextCursor={next_cursor}"

        access_token = gettoken()

        payload = {}
        headers = {
            'WM_SEC.ACCESS_TOKEN': access_token,
            'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
            'WM_SVC.NAME': 'My Walmart Inventory'
        }

        r = requests.get(url, headers=headers)

        root = ET.fromstring(r.content)

        merged_root.append(root)
        print("pause between pricing 200")

        next_cursor_element = root.find('nextCursor')
        if next_cursor_element is not None:
            next_cursor = next_cursor_element.text
        else:
            break

    root = merged_root

    for element in root.iter():
        if element.tag.endswith('sku'):
            sku = element.text
        if element.tag.endswith('amount'):
            amount = element.text
            if sku and amount:
                sku_amount_data.append([sku, amount])

    return sku_amount_data


def writeCSVinventory(sku_quantity_data):
    with open(QTY_OUTPUT_PATH, 'w', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        header = ["sku", "availToSellQty"]
        csv_writer.writerow(header)
        csv_writer.writerows(sku_quantity_data)

    print("Inventory data (sku and availToSellQty) has been written to", QTY_OUTPUT_PATH)


def writeCSVprice(sku_amount_data):
    with open(PRICES_OUTPUT_PATH, 'w', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        csv_writer.writerow(["SKU", "Amount"])
        csv_writer.writerows(sku_amount_data)

    print("CSV file has been created:", PRICES_OUTPUT_PATH)


def writeCSVmerged():
    print("Loading Prices CSV...")
    df_prices = pd.read_csv(PRICES_OUTPUT_PATH, encoding='utf-8', dtype=str)

    print("Loading Qty CSV...")
    df_qty = pd.read_csv(QTY_OUTPUT_PATH, encoding='utf-8', dtype=str)

    print("Merging DataFrames...")
    merged_df = pd.merge(df_qty, df_prices, left_on='sku', right_on='SKU', how='left')

    print("Renaming columns...")
    merged_df.rename(columns={'sku': 'WalmartExportSku'}, inplace=True)

    print("Filling blank values in 'Amount' column with 0...")
    merged_df['Amount'] = merged_df['Amount'].fillna('0')

    print("Dropping 'SKU' column...")
    merged_df.drop(columns=['SKU'], inplace=True)

    print("Saving the merged DataFrame to CSV...")
    merged_df.to_csv(MERGED_OUTPUT_PATH, encoding='utf-8', index=False)

    print(f"Merge complete. The merged dataset has been saved to: {MERGED_OUTPUT_PATH}")


# ── Main flow ──────────────────────────────────────────────────────────────────

# Step 1: Load the previous inventory file BEFORE touching anything else.
# This snapshot is used as the fallback source if the new scrape misses SKUs.
previous_inventory = load_previous_inventory(MERGED_OUTPUT_PATH)

# Step 2: Run the new scrape as usual.
sku_quantity_data = getInventory()
sku_amount_data = getPricing()

# Step 3: Apply fallback logic.
# SKUs confirmed in the new scrape keep their real quantity.
# SKUs that were in the previous file but are missing from this scrape get FALLBACK_QTY.
# Nothing is ever marked in stock based only on incomplete scrape data.
sku_quantity_data = apply_fallback(sku_quantity_data, previous_inventory)

# Step 4: Write outputs (same format as before).
writeCSVinventory(sku_quantity_data)
writeCSVprice(sku_amount_data)
writeCSVmerged()