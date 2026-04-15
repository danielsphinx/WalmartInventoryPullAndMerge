# WalmartInventoryPullAndMerge

Python scripts for pulling Walmart Marketplace inventory and order data, merging inventory and pricing exports, and preparing related CSV outputs.

## Setup

1. Create a Python environment and install the required dependencies used by the scripts:
   - `requests`
   - `pandas`
2. Set the Walmart API authorization header value in an environment variable named `Walmart_Authorization`.

Example PowerShell session:

```powershell
$env:Walmart_Authorization = "Basic <your walmart api credential>"
python .\main.py
```

## Notes

- CSV data exports are ignored by git and are not included in the repository.
- The scripts currently use file names in the project root; adjust paths as needed for your environment.
