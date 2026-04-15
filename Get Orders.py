import csv
import requests
import xml.etree.ElementTree as ET
import os

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

    response = requests.post(url, headers=headers, data=payload)
    if response.status_code == 200:
        root = ET.fromstring(response.text)
        access_token_element = root.find(".//accessToken")
        return access_token_element.text if access_token_element is not None else None
    else:
        print("Error getting token:", response.text)
        return None

import uuid
from datetime import datetime, timedelta, timezone
import requests

def _iso_z(dt: datetime) -> str:
    # Walmart accepts ISO timestamps; using UTC "Z" avoids timezone surprises.
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _fetch_orders_for_status(access_token: str, status: str, created_start: str, created_end: str, limit: int):
    base_url = "https://marketplace.walmartapis.com/v3/orders"

    # First page query
    next_cursor = (
        f"?limit={limit}"
        f"&status={status}"
        f"&createdStartDate={created_start}"
        f"&createdEndDate={created_end}"
    )

    orders = []

    while True:
        url = f"{base_url}{next_cursor}"

        headers = {
            "Accept": "application/json",
            # Content-Type isn't needed for GET, but leaving it harmless is OK:
            # "Content-Type": "application/json",
            "WM_SEC.ACCESS_TOKEN": access_token,
            "WM_QOS.CORRELATION_ID": WM_QOS_CORRELATION_ID,
            "WM_SVC.NAME": "My Walmart Inventory",
        }

        response = requests.get(url, headers=headers, timeout=60)
        if response.status_code != 200:
            print(f"Error fetching orders (status={status}):", response.status_code, response.text)
            break

        response_data = response.json()

        batch = response_data.get("list", {}).get("elements", {}).get("order", []) or []
        orders.extend(batch)

        meta = response_data.get("list", {}).get("meta", {}) or {}
        nc = meta.get("nextCursor")
        if nc:
            # Walmart often returns nextCursor as a querystring (starting with '?')
            next_cursor = nc if str(nc).startswith("?") else ("?" + str(nc))
        else:
            break

    return orders

def get_orders(days_back=14, limit=100):
    access_token = get_token()
    if not access_token:
        print("Failed to get access token")
        return []

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days_back)

    created_start = _iso_z(start_dt)
    created_end = _iso_z(end_dt)

    # “Unshipped” typically includes both:
    # - Created (new/unacknowledged)
    # - Acknowledged
    statuses = ["Created", "Acknowledged"]

    all_orders = []
    seen_po = set()

    for status in statuses:
        batch = _fetch_orders_for_status(
            access_token=access_token,
            status=status,
            created_start=created_start,
            created_end=created_end,
            limit=limit
        )

        # Deduplicate by purchaseOrderId
        for o in batch:
            po = o.get("purchaseOrderId")
            if not po or po in seen_po:
                continue
            seen_po.add(po)
            all_orders.append(o)

    return all_orders



# ...

def get_json_header_csv():
    # Open a CSV file for writing
    with open(
            r'G:\Automation Google Drive\Google Sheet Connected CSVs\Pending Order Exports\Walmart\Raw Walmart Orders With JSON Headers.csv',
            'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = [
            'purchaseOrderId', 'customerOrderId', 'customerEmailId', 'orderDate', 'phone',
            'estimatedDeliveryDate', 'estimatedShipDate', 'methodCode', 'name', 'address1', 'address2',
            'city', 'state', 'postalCode', 'country', 'lineNumber', 'productName', 'sku', 'condition',
            'chargeType', 'chargeName', 'chargeAmount_currency', 'chargeAmount_amount', 'taxName',
            'taxAmount_currency', 'taxAmount_amount', 'unitOfMeasurement', 'orderLineAmount',
            'status', 'statusDate', 'fulfillmentOption', 'shipMethod', 'pickUpDateTime',
            'shippingProgramType'
        ]  # Add other field names as needed

        writer = csv.writer(csvfile)
        writer.writerow(fieldnames)

        # Iterate over each order
        data = get_orders(days_back=14)

        for order in data:
            purchase_order_id = order.get("purchaseOrderId", "")
            customer_order_id = order.get("customerOrderId", "")
            customer_email_id = order.get("customerEmailId", "")
            order_date = order.get("orderDate", "")
            phone = order.get("shippingInfo", {}).get("phone", "")
            estimated_delivery_date = order.get("shippingInfo", {}).get("estimatedDeliveryDate", "")
            estimated_ship_date = order.get("shippingInfo", {}).get("estimatedShipDate", "")
            method_code = order.get("shippingInfo", {}).get("methodCode", "")
            name = order.get("shippingInfo", {}).get("postalAddress", {}).get("name", "")
            address1 = order.get("shippingInfo", {}).get("postalAddress", {}).get("address1", "")
            address2 = order.get("shippingInfo", {}).get("postalAddress", {}).get("address2", "")
            city = order.get("shippingInfo", {}).get("postalAddress", {}).get("city", "")
            state = order.get("shippingInfo", {}).get("postalAddress", {}).get("state", "")
            postal_code = order.get("shippingInfo", {}).get("postalAddress", {}).get("postalCode", "")
            country = order.get("shippingInfo", {}).get("postalAddress", {}).get("country", "")

            # Iterate over each order line
            for order_line in order.get("orderLines", {}).get("orderLine", []):
                line_number = order_line.get("lineNumber", "")
                product_name = order_line.get("item", {}).get("productName", "")
                sku = order_line.get("item", {}).get("sku", "")
                condition = order_line.get("item", {}).get("condition", "")

                # Handle charges and tax details
                charges = order_line.get("charges", {}).get("charge", [])
                charge_type = charges[0].get("chargeType", "") if charges else ""
                charge_name = charges[0].get("chargeName", "") if charges else ""
                charge_amount_currency = charges[0].get("chargeAmount", {}).get("currency", "") if charges else ""
                charge_amount_amount = charges[0].get("chargeAmount", {}).get("amount", "") if charges else ""

                tax = charges[0].get("tax", {}) if charges else {}
                tax_name = tax.get("taxName", "") if tax else ""
                tax_amount_currency = tax.get("taxAmount", {}).get("currency", "") if tax else ""
                tax_amount_amount = tax.get("taxAmount", {}).get("amount", "") if tax else ""

                unit_of_measurement = order_line.get("orderLineQuantity", {}).get("unitOfMeasurement", "")
                order_line_amount = order_line.get("orderLineQuantity", {}).get("amount", "")
                status = order_line.get("orderLineStatuses", {}).get("orderLineStatus", [{}])[0].get("status", "")
                status_date = order_line.get("statusDate", "")
                fulfillment_option = order_line.get("fulfillment", {}).get("fulfillmentOption", "")
                ship_method = order_line.get("fulfillment", {}).get("shipMethod", "")
                pick_up_date_time = order_line.get("fulfillment", {}).get("pickUpDateTime", "")
                shipping_program_type = order_line.get("fulfillment", {}).get("shippingProgramType", "")

                # Create a list for each order line
                row = [
                    purchase_order_id, customer_order_id, customer_email_id, order_date, phone,
                    estimated_delivery_date, estimated_ship_date, method_code, name, address1, address2,
                    city, state, postal_code, country, line_number, product_name, sku, condition,
                    charge_type, charge_name, charge_amount_currency, charge_amount_amount, tax_name,
                    tax_amount_currency, tax_amount_amount, unit_of_measurement, order_line_amount,
                    status, status_date, fulfillment_option, ship_method, pick_up_date_time,
                    shipping_program_type
                ]

                # Write the row to the CSV file
                writer.writerow(row)

    print(
        r'G:\Automation Google Drive\Google Sheet Connected CSVs\Pending Order Exports\Walmart\Raw Walmart Orders With JSON Headers.csv csv created')


# ...

def map_to_second_format():
    # Open a CSV file for writing
    with open(r'G:\Automation Google Drive\Google Sheet Connected CSVs\Pending Order Exports\Walmart\Walmart Orders With Order Bot Headers.csv', 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = [
            'Order ID', 'Date', 'Buyer', 'Price', 'Tax', 'Tax Remitted', 'Shipping', 'Shipping Type',
            'Products', 'Quantity', 'Retail ID', 'Street 1', 'Street 2', 'City', 'State', 'ZIP Code',
            'Merchant Order Reference', 'Order Status', 'Invoice origin', 'Receiver ID', 'Country'
        ]  # Add other field names as needed

        writer = csv.writer(csvfile)
        writer.writerow(fieldnames)

        # Iterate over each order
        data = get_orders()  # Fetch orders
        for order in data:
            order_id = order.get("purchaseOrderId", "")
            order_date = order.get("orderDate", "")
            name = order.get("shippingInfo", {}).get("postalAddress", {}).get("name", "")

            # Process each order line
            for order_line in order.get("orderLines", {}).get("orderLine", []):
                charges = order_line.get("charges", {}).get("charge", [])
                price = order_line.get("charges", {}).get("charge", [{}])[0].get("chargeAmount", {}).get("amount", "")
                tax = charges[0].get("tax", {}) if charges else {}
                tax = tax.get("taxAmount", {}).get("amount", "") if tax else ""
                shipping_method = order.get("shippingInfo", {}).get("methodCode", "")
                product_name = order_line.get("item", {}).get("productName", "")
                quantity = order_line.get("orderLineQuantity", {}).get("amount", "")
                sku = order_line.get("item", {}).get("sku", "")
                address1 = order.get("shippingInfo", {}).get("postalAddress", {}).get("address1", "")
                address2 = order.get("shippingInfo", {}).get("postalAddress", {}).get("address2", "")
                city = order.get("shippingInfo", {}).get("postalAddress", {}).get("city", "")
                state = order.get("shippingInfo", {}).get("postalAddress", {}).get("state", "")
                postal_code = order.get("shippingInfo", {}).get("postalAddress", {}).get("postalCode", "")
                order_statuses = order_line.get("orderLineStatuses", {}).get("orderLineStatus", [{}])
                order_status = order_statuses[0].get("status", "") if order_statuses else ""
                country = order.get("shippingInfo", {}).get("postalAddress", {}).get("country", "")

                # Create a list for each order line with the new headers
                row = [
                    order_id, order_date, name, price, tax, "", shipping_method, "",
                    product_name, quantity, sku, address1, address2, city, state, postal_code,
                    "", order_status, "", "", country
                ]

                # Write the row to the CSV file
                writer.writerow(row)

    print(r'G:\Automation Google Drive\Google Sheet Connected CSVs\Pending Order Exports\Walmart\Walmart Orders With Order Bot Headers.csv csv created')
"G:\Documents And Logs"
# Clear the lists before processing new orders
all_item_info = []
all_order_bot_item_info = []

# Process orders and write to CSV
get_json_header_csv()
map_to_second_format()
