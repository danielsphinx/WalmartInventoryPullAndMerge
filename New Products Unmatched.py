import requests
import json
import xml.etree.ElementTree as ET
import pandas as pd
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

    print(response.text)

    root = ET.fromstring(response.text)
    access_token_element = root.find(".//accessToken")

    access_token = access_token_element.text
    return access_token

def new_products_full_data(csv_file_path):
    # Read the product data from the CSV file into a DataFrame
    product_data = pd.read_csv(csv_file_path)
    product_data = product_data[product_data['UpperCaseCategory'].notna() & (product_data['UpperCaseCategory'] != "")].copy()

    print(product_data)

    # Set the batch size
    batch_size = 1000

    # Call the function to update product information in batches
    for i in range(0, len(product_data), batch_size):
        # Get a batch of rows
        batch = product_data[i:i+batch_size]

        # Call the function to process the current batch
        process_batch(batch)


def process_batch(batch):
    url = "https://marketplace.walmartapis.com/v3/feeds?feedType=MP_ITEM"

    access_token = get_token()

    mp_items = []

    for index, row in batch.iterrows():
        sku = row['sku']
        product_name = row['productName']
        short_description = row['shortDescription']
        main_image_url = row['mainImageUrl']
        price = row['price']
        msrp = row['msrp']
        quantity = row['quantity']
        product_id = 'CUSTOM' if '-B-' in sku else str(int(row['productId'])).rjust(14, '0')
        print(product_id)
        shipping_weight = row['ShippingWeight']
        brand = row['brand']
        physical_media_format = row['physicalMediaFormat']
        lower_case_category = row['LowerCaseCategory']
        upper_case_category = row['UpperCaseCategory']
        secondary_image_urls = row['productSecondaryImageURL']

        # Extract key features
        key_features = [
            row.get('Metafield: my_fields.feature_one [single_line_text_field]', ''),
            row.get('Metafield: my_fields.feature_two [single_line_text_field]', ''),
            row.get('Metafield: my_fields.feature_three [single_line_text_field]', ''),
            row.get('Metafield: my_fields.feature_four [single_line_text_field]', ''),
            row.get('Metafield: my_fields.feature_five [single_line_text_field]', ''),
            f"Format: {physical_media_format}",
            f"Artist: {brand}"
        ]

        # Filter out any empty strings or NaN values from key features
        key_features = [feature for feature in key_features if pd.notna(feature) and feature]
        print(key_features)  # Debug print to check filtered key features

        # Convert secondary_image_urls to a list of strings
        if pd.notna(secondary_image_urls):
            secondary_image_urls = [url.strip() for url in secondary_image_urls.split(';')]
        else:
            secondary_image_urls = None


        # Create the JSON structure for the current product
        product_json = {
            "Orderable": {
                "sku": sku,
                "productIdentifiers": {
                    "productIdType": "GTIN",
                    "productId": str(product_id)
                },
                "productName": product_name,
                "brand": brand,
                "price": price,
                "ShippingWeight": shipping_weight,
                "MustShipAlone": "No"
            },
            "Visible": {
                upper_case_category: {
                    "shortDescription": short_description,
                    "mainImageUrl": main_image_url,
                    "keyFeatures": key_features,
                    "physicalMediaFormat": [physical_media_format],
                    "performer": [brand]
                }
            }
        }

        # Conditionally include the productSecondaryImageURL field
        if secondary_image_urls:
            product_json["Visible"][upper_case_category]["productSecondaryImageURL"] = secondary_image_urls

        # Append the product JSON to the list
        mp_items.append(product_json)

    # Construct the complete payload
    payload = {
        "MPItemFeedHeader": {
            "processMode": "REPLACE",
            "subset": "EXTERNAL",
            "locale": "en",
            "sellingChannel": "marketplace",
            "version": "4.8",
            "subCategory": "music"
        },
        "MPItem": mp_items
    }

    payload_json = json.dumps(payload)

    # Send the POST request to update the product information
    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': 'f025d437-70d9-45fb-9c56-1a580c4fcdfb',
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json'
    }

    response = requests.post(url, headers=headers, data=payload_json)

    #  print(payload_json)
    print("Update Response:", response.text)


# Call the main function with the CSV file path
#new_products_full_data(r'G:\Automation Google Drive\Wholesale UI CSVs\Merged Browser Uploads\Walmart\New Merged New For Walmart Unmatched.csv')
#new_products_full_data(r'\\Truenas\Offline Files Backed Up\Automation Google Drive\Wholesale UI CSVs\DUPS AND BUNDLES\AMS\Walmart Files\New.csv')
