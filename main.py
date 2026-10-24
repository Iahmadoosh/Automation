import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from adapters.linkedin import LinkedInAdapter
from engine.evaluator import JobEvaluator

BASE_DIR = Path(__file__).resolve().parent
PROFILE_PATH = BASE_DIR / "data" / "profile.json"
AUTH_PATH = BASE_DIR / "data" / "auth.json"
RESUME_PATH = BASE_DIR / "assets" / "resume.pdf"

def main():
    if not PROFILE_PATH.exists() or not AUTH_PATH.exists():
        print("Missing profile.json or auth.json")
        return

    with open(PROFILE_PATH, "r", encoding="utf-8") as f:
        profile_data = json.load(f)

    # Evaluator now loads your resume PDF directly from assets/
    evaluator = JobEvaluator(profile_data, RESUME_PATH)

    print("\n" + "=" * 55)
    greeting = evaluator.ask_setup_questions()
    print(f"Qwen: {greeting}\n")

    user_goal = input("Target Job/Role (e.g., 'retail', 'sales associate'): ").strip()
    if not user_goal:
        user_goal = "Retail Sales Associate"

    user_location = input("Target Location (e.g., 'Ottawa, ON', 'Canada'): ").strip()
    if not user_location:
        user_location = "Canada"

    commitment = input("Commitment [Part-time / Full-time / Any]: ").strip()
    if not commitment:
        commitment = "Any"
    print("=" * 55)

    print("\nQwen is analyzing your resume and configuring search queries...")
    matrix = evaluator.generate_search_matrix(user_goal, user_location, commitment)
    location = matrix.get("location", user_location)
    job_type_code = matrix.get("job_type_code", "")
    queries = matrix.get("queries", [user_goal])

    print(f"\nTarget Location: {location}")
    print(f"Schedule Filter Code: '{job_type_code or 'Any'}'")
    print("Queries to execute:")
    for idx, q in enumerate(queries, 1):
        print(f"  {idx}. {q}")
    print("-" * 55)

    seen_jobs = set()
    matches_found = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=100)
        context = browser.new_context(storage_state=AUTH_PATH)
        page = context.new_page()

        bot = LinkedInAdapter(page, profile_data)

        if not bot.verify_login():
            print("Session expired or invalid.")
            browser.close()
            return

        for query in queries:
            print(f"\n==============================================")
            print(f"Searching: '{query}' in '{location}' ({commitment})")
            print(f"==============================================")
            bot.search_jobs(keywords=query, location=location, job_type_code=job_type_code)

            card_locator = page.locator("li[data-occludable-job-id], div.job-card-container")
            try:
                card_locator.first.wait_for(state="visible", timeout=8000)
            except Exception:
                print(f"No listings found for '{query}'")
                continue

            max_jobs_per_query = 15
            for i in range(max_jobs_per_query):
                try:
                    cards = page.locator("li[data-occludable-job-id], div.job-card-container")
                    if i >= cards.count():
                        page.evaluate("""() => {
                            const pane = document.querySelector('.jobs-search-results-list') || window;
                            pane.scrollBy(0, 500);
                        }""")
                        page.wait_for_timeout(1500)
                        cards = page.locator("li[data-occludable-job-id], div.job-card-container")
                        if i >= cards.count():
                            break

                    card = cards.nth(i)
                    card.scroll_into_view_if_needed()
                    card.click()
                    page.wait_for_timeout(1500)

                    details = bot.get_current_job_details()
                    job_key = f"{details['title']} - {details['company']}"
                    if job_key in seen_jobs:
                        continue
                    seen_jobs.add(job_key)

                    print(f"\n[{len(seen_jobs)}] Evaluating: '{details['title']}' at '{details['company']}'")

                    decision = evaluator.evaluate(
                        job_title=details["title"],
                        company=details["company"],
                        job_description=details["description"],
                        user_intent=user_goal,
                        commitment_type=commitment
                    )

                    is_match = decision.get("match", False)
                    reason = decision.get("reason", "No reason provided")
                    conf = decision.get("confidence", 0)

                    print(f"-> Decision: {'MATCH' if is_match else 'REJECT'} ({conf}%) | {reason}")

                    if is_match:
                        matches_found += 1
                        print(f"*** Match #{matches_found} found! ***")
                        if bot.click_easy_apply():
                            print("Easy Apply modal opened.")
                            page.wait_for_timeout(2000)
                            bot.close_modal_if_open()

                    page.wait_for_timeout(1000)

                except Exception as loop_err:
                    print(f"Warning during job processing: {loop_err}")
                    bot.close_modal_if_open()
                    continue

        print(f"\nDone. Evaluated: {len(seen_jobs)} postings. Total matches: {matches_found}")
        page.wait_for_timeout(3000)
        browser.close()

if __name__ == "__main__":
    main()