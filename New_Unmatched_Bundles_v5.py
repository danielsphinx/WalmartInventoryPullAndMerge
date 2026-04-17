import requests
import json
import xml.etree.ElementTree as ET
import pandas as pd
import os
import math

Walmart_Authorization = os.getenv('Walmart_Authorization')
WM_QOS_CORRELATION_ID = os.getenv('WM_QOS_CORRELATION_ID')
if not Walmart_Authorization:
    raise Exception("Walmart_Authorization environment variable is not set!")


def clean_nans(obj):
    """Recursively remove NaN/None values from dicts/lists so JSON stays valid.
    NaN in JSON is not valid and causes 'Invalid JSON payload' errors."""
    if isinstance(obj, dict):
        cleaned = {}
        for k, v in obj.items():
            cleaned_v = clean_nans(v)
            if cleaned_v is not None:
                cleaned[k] = cleaned_v
        return cleaned
    elif isinstance(obj, list):
        return [clean_nans(v) for v in obj if clean_nans(v) is not None]
    elif isinstance(obj, float) and (math.isnan(obj) or pd.isna(obj)):
        return None
    elif obj is pd.NA or obj is None:
        return None
    return obj


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
            "country_of_origin_substantial_transformation": "United States"
        }

        if pd.notna(msrp):
            orderable["msrp"] = msrp

        # --- Visible section ---
        music_visible = {
            "productName": product_name,
            "brand": brand,
            "shortDescription": short_description,
            "mainImageUrl": main_image_url,
            "keyFeatures": key_features,
            "physicalMediaFormat": [physical_media_format],
            "performer": [brand],
            "isProp65WarningRequired": "No",
            "condition": "New",
            "has_written_warranty": "No",
            "isCollectible": "No",
            "netContent": {
                "productNetContentMeasure": 1,
                "productNetContentUnit": "Each"
            }
        }

        if secondary_image_urls:
            music_visible["productSecondaryImageURL"] = secondary_image_urls

        product_json = {
            "Orderable": orderable,
            "Visible": {
                "Music": music_visible
            }
        }

        mp_items.append(product_json)

    if not mp_items:
        print("No valid items to submit in this batch.")
        return

    payload = {
        "MPItemFeedHeader": {
            "businessUnit": "WALMART_US",
            "locale": "en",
            "version": "5.0.20260114-19_40_57-api"
        },
        "MPItem": mp_items
    }

    # Clean NaN values — NaN in JSON is not valid and causes "Invalid JSON payload"
    payload = clean_nans(payload)

    payload_json = json.dumps(payload)

    # Debug: print the full payload so we can see exactly what's being sent
    print("\n=== FULL PAYLOAD ===")
    print(json.dumps(payload, indent=2))
    print("=== END PAYLOAD ===\n")

    headers = {
        'WM_SEC.ACCESS_TOKEN': access_token,
        'WM_QOS.CORRELATION_ID': WM_QOS_CORRELATION_ID,
        'WM_SVC.NAME': 'Walmart Marketplace',
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }

    response = requests.post(url, headers=headers, data=payload_json)
    print("Update Response:", response.text)


# Call the main function with the CSV file path
new_products_full_data(r'\\Truenas\Offline Files Backed Up\Automation Google Drive\Wholesale UI CSVs\DUPS AND BUNDLES\AMS\Walmart Files\New.csv')