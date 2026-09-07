import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from adapters.linkedin import LinkedInAdapter
from engine.evaluator import JobEvaluator

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "data" / "profile.json"
AUTH_PATH = BASE_DIR / "data" / "auth.json"

def main():
    if not PROFILE_PATH.exists() or not AUTH_PATH.exists():
        print("Missing profile.json or auth.json")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    evaluator = JobEvaluator(profile_data)

    # 1. Interactive terminal prompt
    print("\n" + "=" * 50)
    user_goal = input("What kind of job are you looking for today?\n> ").strip()
    if not user_goal:
        user_goal = "Retail or customer service jobs"
    print("=" * 50)

    # 2. Qwen determines best keywords and search terms
    print("\nQwen is analyzing your request against your profile...")
    search_params = evaluator.parse_search_intent(user_goal)
    keywords = search_params.get("keywords", user_goal)
    location = search_params.get("location", "Canada")
    print(f"Search Plan: Keywords='{keywords}' | Location='{location}'\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(storage_state=AUTH_PATH)
        page = context.new_page()

        bot = LinkedInAdapter(page, profile_data)

        if not bot.verify_login():
            print("Session expired or invalid.")
            browser.close()
            return

        # 3. Navigate with the parameters chosen by Qwen
        bot.search_jobs(keywords=keywords, location=location)

        # 4. Select listing and evaluate
        if bot.select_first_job():
            details = bot.get_current_job_details()
            print(f"\nEvaluating: '{details['title']}' at '{details['company']}'")

            decision = evaluator.evaluate(
                job_title=details["title"],
                company=details["company"],
                job_description=details["description"],
                user_intent=user_goal
            )
            print(f"\nDecision Result: {decision}")

            if decision.get("match"):
                print("\nJob approved! Proceeding to apply...")
                bot.click_easy_apply()
            else:
                print(f"\nJob skipped: {decision.get('reason')}")
        else:
            print("No job cards available to inspect.")

        page.wait_for_timeout(5000)
        browser.close()

if __name__ == "__main__":
    main()