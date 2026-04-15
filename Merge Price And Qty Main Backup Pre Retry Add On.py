import requests
import xml.etree.ElementTree as ET
import csv
import json
import pandas as pd
import time
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')


def gettoken():
    url = "https://marketplace.walmartapis.com/v3/token"

    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = requests.request("POST", url, headers=headers, data=payload)

    print(response.text)

    # Parse the XML response using xml.etree.ElementTree
    root = ET.fromstring(response.text)

    # Find the 'accessToken' element
    access_token_element = root.find(".//accessToken")

    access_token = access_token_element.text
    return (access_token)
# Step 2: Make paginated requests for inventory data

def getInventory():
    sku_quantity_data = []
    merged_inventory_data = []
    next_cursor = None

    while True:
        url = "https://marketplace.walmartapis.com/v3/inventories?limit=50"

        if next_cursor is not None:
            url += f"&nextCursor={next_cursor}"

        # Obtain the access token as you did before
        access_token = gettoken()

        headers = {
            'WM_SEC.ACCESS_TOKEN': access_token,
            'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
            'WM_SVC.NAME': 'YOUR_SERVICE_NAME',
            'Accept': 'application/json'
        }

        response = requests.get(url, headers=headers)
        inventory_info = response.json()
        discogs_type_inventory_info = response.text

        # Load the JSON data
        data = json.loads(discogs_type_inventory_info)

        # Get a list of all keys (tags) in the JSON data
        all_keys = []

        def get_keys(obj):
            if isinstance(obj, dict):
                for key, value in obj.items():
                    all_keys.append(key)
                    get_keys(value)
            elif isinstance(obj, list):
                for item in obj:
                    get_keys(item)

        get_keys(data)

        # Print the list of all keys
        #print("All keys in the JSON data:", all_keys)

        # Append the inventory data to the list
        merged_inventory_data.extend(inventory_info['elements']['inventories'])
        # print("merged it!")
        time.sleep(.5)
        print("pause between inventory 50")

        # Handle the inventory data here...

        if 'nextCursor' in inventory_info.get('meta', {}):
            next_cursor = inventory_info['meta']['nextCursor']
            # print("Next Cursor", next_cursor)
        else:
            break  # No more pages to fetch

    # Continue processing or saving the inventory data...
    # Iterate through the all_inventory_data and write sku and availToSellQty for each row
    for inventory_item in merged_inventory_data:
        sku = inventory_item.get('sku', "")  # Use "" as the default value for sku
        avail_to_sell_qty = 0  # Initialize avail_to_sell_qty to 0 by default

        # Check if 'nodes' exists in inventory_item
        if 'nodes' in inventory_item:
            for node in inventory_item['nodes']:
                avail_to_sell_qty = node['availToSellQty'].get('amount', 0) if 'availToSellQty' in node else 0

        # print("SKU:", sku)
        # print("AvailToSellQty:", avail_to_sell_qty)
        sku_quantity_data.append([sku, avail_to_sell_qty])


    # print(merged_inventory_data)
    return sku_quantity_data


sku_quantity_data = getInventory()


def getPricing():
    # Create an empty list to store SKU and amount data
    sku_amount_data = []
    # Create an empty XML document to store merged responses
    merged_root = ET.Element('ItemResponses', {'xmlns:wmSpecs': 'http://walmart.com'})
    # create empty list for news items

    # Create an empty XML document to store merged responses
    merged_xml = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><ItemResponses xmlns:wmSpecs="http://walmart.com" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ns2="http://walmart.com"/>'

    # Step 2: Make paginated requests for price data
    next_cursor = '*'

    while True:
        url = f"https://marketplace.walmartapis.com/v3/items?limit=200&nextCursor={next_cursor}"

        # Obtain the access token as you did before
        access_token = gettoken()

        payload = {}
        headers = {
            'WM_SEC.ACCESS_TOKEN': access_token,
            'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
            'WM_SVC.NAME': 'My Walmart Inventory'
        }
        # print("this is the searched url ", url)
        r = requests.get(url, headers=headers)

        # print(r.content)

        # create element tree object
        root = ET.fromstring(r.content)

        # for child in root.iter('*'):
        # print(child.tag)

        # Append the response XML to the merged_root tree
        merged_root.append(root)
        time.sleep(1.5)
        print("pause between pricing 200")

        next_cursor_element = root.find('nextCursor')
        if next_cursor_element is not None:
            next_cursor = next_cursor_element.text
            # print("nextCursor:", next_cursor)


        else:

            break  # No more pages to fetch

    root = merged_root
    # print(merged_root)

    # ... (your existing code to fetch and parse XML)

    # Inside your loop where you extract SKU and amount, append them to the list
    for element in root.iter():
        if element.tag.endswith('sku'):
            sku = element.text
        if element.tag.endswith('amount'):
            amount = element.text
            if sku and amount:
                sku_amount_data.append([sku, amount])

    return (sku_amount_data)
sku_amount_data = getPricing()


def writeCSVinventory():
    # Your code to fetch and append inventory data to all_inventory_data

    # Define the path to the CSV file
    csv_file_path = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Quantity Export.csv'

    # Open the CSV file for writing
    with open(csv_file_path, 'w', newline='') as csv_file:
        # Create a CSV writer object
        csv_writer = csv.writer(csv_file)

        # Write the header row
        header = ["sku", "availToSellQty"]  # Define the columns you want to write
        csv_writer.writerow(header)

        # Write the data for each row
        csv_writer.writerows(sku_quantity_data)

    print("Inventory data (sku and availToSellQty) has been written to", csv_file_path)
writeCSVinventory()


def writeCSVprice():
    # Define the CSV file path
    csv_file_path = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Prices Export.csv'

    # Write the data to a CSV file
    with open(csv_file_path, 'w', newline='') as csv_file:
        csv_writer = csv.writer(csv_file)
        # Write the header
        csv_writer.writerow(["SKU", "Amount"])
        # Write the data
        csv_writer.writerows(sku_amount_data)

    print("CSV file has been created:", csv_file_path)
    # This code will extract the "sku" and "amount" data and save it to a CSV file with headers "SKU" and "Amount." Make sure to adjust the file path and other parts of the code according to your needs.
writeCSVprice()


def writeCSVmerged():
    # Paths to the CSV files
    walmart_prices_export = r"G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Prices Export.csv"
    walmart_qty_export = r"G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Walmart Quantity Export.csv"

    merged_df_path = r'G:\Automation Google Drive\Marketplace Exported Inventory\Walmart Full Inventory Exports\API Python Generated Merged Exports\Merged Price And Quantity Walmart Export.csv'  # Adjust the output path as needed

    # Load the first CSV file, treating all data as strings
    print("Loading Prices CSV...")
    df_prices = pd.read_csv(walmart_prices_export, encoding='utf-8', dtype=str)

    # Load the second CSV file, treating all data as strings
    print("Loading Qty CSV...")
    df_qty = pd.read_csv(walmart_qty_export, encoding='utf-8', dtype=str)


    # Merge the filtered Ingram dataframe with the Gardners dataframe on the modified columns
    print("Merging DataFrames...")
    merged_df = pd.merge(df_qty, df_prices, left_on='sku', right_on='SKU', how='left')

    # Rename the 'sku' column to 'WalmartExportSku'
    print("Renaming columns...")
    merged_df.rename(columns={'sku': 'WalmartExportSku'}, inplace=True)

    # Replace blank values in 'Amount' column with 0
    print("Filling blank values in 'Amount' column with 0...")
    merged_df['Amount'] = merged_df['Amount'].fillna('0')

    # Drop the 'SKU' column
    print("Dropping 'SKU' column...")
    merged_df.drop(columns=['SKU'], inplace=True)

    # Save the merged dataframe to a new CSV file
    print("Saving the merged DataFrame to CSV...")
    merged_df.to_csv(merged_df_path, encoding='utf-8', index=False)

    print(f"Merge complete. The merged dataset has been saved to: {merged_df_path}")

writeCSVmerged()


