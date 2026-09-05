from playwright.sync_api import sync_playwright
def run():
    # Starting playwright
    with sync_playwright() as p:
        # Launching the browser
        browser = p.chromium.launch(headless = False, slow_mo = 100)

        # Creating an isolated context and a new tab
        context = browser.new_context()
        page = context.new_page()

        # Navigating to the URL
        print("Navigating to the URL...")
        page.goto("https://demo.playwright.dev/todomvc/")

        # finding the input field
        print("Finding the input field...")
        input_field = page.get_by_placeholder("What needs to be done?")

        # Filling the input field
        input_field.fill("get a job!")
        input_field.press("Enter")
        page.wait_for_timeout(3000)


        browser.close()

run()