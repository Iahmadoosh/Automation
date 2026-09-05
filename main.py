import json
from pathlib import Path
from playwright.sync_api import sync_playwright


baseDir = Path(__file__).resolve().parent
profilePath = baseDir / "data" / "profile.json"
with open(profilePath, 'r', encoding= 'utf-8') as file:
    data = json.load(file)

    firstName = data["first_name"]
    lastName = data["last_name"]
    email = data["email"]
    phone = data["phone"]
    linkedin = data["linkedin"]
    github = data["github"]

    print(f"Loaded profile for {firstName} {lastName}")




