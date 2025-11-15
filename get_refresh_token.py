import json
import os

PATH = "tokens/user_token.json"

if not os.path.exists(PATH):
    print(f"Token file not found: {PATH}")
    exit(1)

with open(PATH, "r") as f:
    data = json.load(f)

refresh = data.get("refresh_token")

if refresh:
    print("REFRESH TOKEN:")
    print(refresh)
else:
    print("No refresh_token found in user_token.json")
