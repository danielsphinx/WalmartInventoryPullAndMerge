import os

# Fetch the Walmart_Authorization environment variable
Walmart_Authorization = os.getenv('Walmart_Authorization')

# Specify the file path to write to
file_path = 'output.txt'

# Open the file and write the environment variable's value
with open(file_path, 'w') as file:
    file.write(str(Walmart_Authorization))  # Convert None to 'None' if Walmart_Authorization is not set

print("Value written to file:", Walmart_Authorization)
