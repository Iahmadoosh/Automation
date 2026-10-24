import urllib.parse

class LinkedInAdapter:
    def __init__(self, page, profile: dict):
        self.page = page
        self.profile = profile

    def verify_login(self) -> bool:
        print("Verifying session...")
        self.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded")
        self.page.wait_for_timeout(2000)
        if "/feed" in self.page.url:
            print("Session authenticated successfully!")
            return True
        return False

    def search_jobs(self, keywords: str, location: str, job_type_code: str = ""):
        params = {
            "keywords": keywords,
            "location": location,
            "f_AL": "true"  # Easy Apply only
        }
        if job_type_code:
            params["f_JT"] = job_type_code  # 'F' for Full-time, 'P' for Part-time

        query_string = urllib.parse.urlencode(params)
        search_url = f"https://www.linkedin.com/jobs/search/?{query_string}"
        
        print(f"Navigating to: {search_url}")
        self.page.goto(search_url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(3000)
    def select_first_job(self) -> bool:
        print("Waiting for job listings to load...")
        
        # Selector list covering LinkedIn's recent DOM variations
        card_selectors = [
            "li[data-occludable-job-id]",
            "div.job-card-container",
            "li.jobs-search-results__list-item",
            ".scaffold-layout__list-item",
            "div[data-job-id]"
        ]
        
        combined_selector = ", ".join(card_selectors)
        job_card = self.page.locator(combined_selector).first

        try:
            job_card.wait_for(state="visible", timeout=12000)
            job_card.scroll_into_view_if_needed()
            job_card.click()
            print("Selected first job card.")
            self.page.wait_for_timeout(2000)
            return True
        except Exception as e:
            print(f"Could not locate job cards: {e}")
            return False

    def click_easy_apply(self) -> bool:
        print("Looking for Easy Apply button...")

        # 1. Scope directly to the job details view container on the right
        # This prevents picking up the top search filter bar completely.
        apply_btn = self.page.locator(
            ".jobs-search__job-details--container button.jobs-apply-button, "
            ".job-view-layout button.jobs-apply-button"
        ).first

        # 2. Alternatively, Playwright's role locator scoped to the details section:
        if not apply_btn.is_visible():
            apply_btn = self.page.locator(".jobs-search__job-details--container").get_by_role(
                "button", name="Easy Apply"
            ).first

        # Wait briefly for it to render
        try:
            apply_btn.wait_for(state="visible", timeout=5000)
        except Exception:
            print("Easy Apply button did not appear within 5 seconds.")
            return False

        if apply_btn.is_enabled():
            print("Found application button! Clicking Easy Apply...")
            apply_btn.click()
            self.page.wait_for_timeout(2000)
            return True
        else:
            print("Easy Apply button found, but it is disabled or already applied.")
            return False

    def get_current_job_details(self) -> dict:
        """Extracts title, company, and job description from the active job details pane."""
        print("Extracting job details for evaluation...")

        # 1. Title
        title_elem = self.page.locator(
            ".job-details-jobs-unified-top-card__job-title, "
            ".jobs-unified-top-card__job-title, "
            "h1"
        ).first
        title = title_elem.inner_text().strip() if title_elem.is_visible() else "Unknown Title"

        # 2. Company
        company_elem = self.page.locator(
            ".job-details-jobs-unified-top-card__company-name, "
            ".jobs-unified-top-card__company-name"
        ).first
        company = company_elem.inner_text().strip() if company_elem.is_visible() else "Unknown Company"

        # 3. Description
        desc_elem = self.page.locator("#job-details, .jobs-description__content").first
        description = desc_elem.inner_text().strip() if desc_elem.is_visible() else ""

        return {
            "title": title,
            "company": company,
            "description": description
        }

    def close_modal_if_open(self):
        """Closes any open application modal to unblock the page."""
        dismiss_btn = self.page.locator(
            "button[aria-label='Dismiss'], button[data-control-name='overlay.close_padding']"
        ).first
        if dismiss_btn.is_visible():
            dismiss_btn.click()
            self.page.wait_for_timeout(1000)

            # LinkedIn often pops a 'Discard changes?' confirmation dialog
            discard_btn = self.page.locator("button[data-control-name='discard_application_confirm_btn']").first
            if discard_btn.is_visible():
                discard_btn.click()
                self.page.wait_for_timeout(1000)