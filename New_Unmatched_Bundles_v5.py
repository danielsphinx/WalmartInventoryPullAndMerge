import requests
import json
import xml.etree.ElementTree as ET
import pandas as pd
import os

Walmart_Authorization = os.getenv('Walmart_Authorization')
WM_QOS_CORRELATION_ID = os.getenv('WM_QOS_CORRELATION_ID')
if not Walmart_Authorization:
    raise Exception("Walmart_Authorization environment variable is not set!")


def get_token():
    url = "https://marketplace.walmartapis.com/v3/token"
    payload = 'grant_type=client_credentials'
    headers = {
        'Authorization': Walmart_Authorization,
        'Content-Type': 'application/x-www-form-urlencoded',
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'My Walmart Inventory'
    }

    response = requests.request("POST", url, headers=headers, data=payload)
    print(f"Token Response Status: {response.status_code}")
    print(response.text)

    if response.status_code != 200:
        raise Exception(f"Token request failed with status {response.status_code}: {response.text}")

    # Try JSON first, fall back to XML (token endpoint may return either)
    try:
        token_data = response.json()
        access_token = token_data['access_token']
    except (requests.exceptions.JSONDecodeError, ValueError, KeyError):
        root = ET.fromstring(response.text)
        access_token_element = root.find(".//accessToken")
        access_token = access_token_element.text

    return access_token


def new_products_full_data(csv_file_path):
    product_data = pd.read_csv(csv_file_path)
    # --- CHANGED: No longer filtering by UpperCaseCategory since all items go to "Music" product type ---
    # Keep the filter in case there are blank rows you want to skip
    product_data = product_data[product_data['sku'].notna() & (product_data['sku'] != "")].copy()
    print(product_data)

    batch_size = 1000

    for i in range(0, len(product_data), batch_size):
        batch = product_data[i:i+batch_size]
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
        shipping_weight = row['ShippingWeight']
        brand = row['brand']
        physical_media_format = row['physicalMediaFormat']
        secondary_image_urls = row['productSecondaryImageURL']

        # Product ID handling — same logic as before.
        # Bundles (SKUs with '-B-') use "CUSTOM" as the productId VALUE
        # (requires GTIN exemption approved in Seller Center).
        # Non-bundles use the UPC/GTIN padded to 14 digits.
        product_id = 'CUSTOM' if '-B-' in sku else str(int(row['productId'])).rjust(14, '0')
        print(f"SKU: {sku}, ProductID: {product_id}")

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
        key_features = [f for f in key_features if pd.notna(f) and f]
        print(key_features)

        # Convert secondary image URLs to list
        if pd.notna(secondary_image_urls):
            secondary_image_urls = [url.strip() for url in secondary_image_urls.split(';')]
        else:
            secondary_image_urls = None

        # =====================================================================
        # BUILD THE 5.0 PAYLOAD STRUCTURE
        # =====================================================================

        # --- Orderable section ---
        orderable = {
            "sku": sku,
            "productIdentifiers": {
                "productIdType": "GTIN",
                "productId": product_id
            },
            "price": price,
            "ShippingWeight": shipping_weight,
            "MustShipAlone": "No",
            # --- NEW REQUIRED FIELD ---
            # Set to the country where your product is manufactured/produced.
            # Change "United States" if your items come from elsewhere.
            "country_of_origin_substantial_transformation": "United States"
        }

        # Add msrp if present
        if pd.notna(msrp):
            orderable["msrp"] = msrp

        # --- Visible section ---
        # In 5.0, the product type key is "Music" (not the old dynamic category name).
        # productName and brand have moved here from Orderable.
        # Several new fields are now REQUIRED.
        music_visible = {
            "productName": product_name,
            "brand": brand,
            "shortDescription": short_description,
            "mainImageUrl": main_image_url,
            "keyFeatures": key_features,
            "physicalMediaFormat": [physical_media_format],
            "performer": [brand],

            # --- NEW REQUIRED FIELDS in 5.0 ---
            "isProp65WarningRequired": "No",
            "condition": "New",
            "has_written_warranty": "No",
            "isCollectible": "No",
            "netContent": {
                "productNetContentMeasure": 1,
                "productNetContentUnit": "Each"
            }
        }

        # Add secondary images if present
        if secondary_image_urls:
            music_visible["productSecondaryImageURL"] = secondary_image_urls

        # Assemble the full item
        product_json = {
            "Orderable": orderable,
            "Visible": {
                "Music": music_visible        # <-- CHANGED: Always "Music", not dynamic category
            }
        }

        mp_items.append(product_json)

    if not mp_items:
        print("No valid items to submit in this batch.")
        return

    # --- CHANGED: New MPItemFeedHeader for spec 5.0 ---
    # Removed: processMode, subset, sellingChannel, subCategory
    # Added: businessUnit
    # Changed: version to 5.0 spec string
    payload = {
        "MPItemFeedHeader": {
            "businessUnit": "WALMART_US",
            "locale": "en",
            "version": "5.0.20260114-19_40_57-api"
        },
        "MPItem": mp_items
    }

    payload_json = json.dumps(payload)

    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json',
        'Accept': 'application/json'           # <-- NEW: explicitly request JSON response
    }

    response = requests.post(url, headers=headers, data=payload_json)
    print("Update Response:", response.text)


# Call the main function with the CSV file path
new_products_full_data(r'\\Truenas\Offline Files Backed Up\Automation Google Drive\Wholesale UI CSVs\DUPS AND BUNDLES\AMS\Walmart Files\New - Copy.csv')
