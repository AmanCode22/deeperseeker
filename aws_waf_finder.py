import json
from datetime import datetime

from playwright._impl._errors import Error as Error_Playwright
from playwright.sync_api import expect, sync_playwright

with sync_playwright() as p:
    try:
        browser = p.chromium.launch(headless=True)

    except Error_Playwright as e:
        if "Executable doesn't exist" in str(e):
            print(
                "Please run: playwright install chromium. You may not have installed chromeium driver of playwright for more see docs."
            )
        else:
            print("Browser driver issue:", str(e))
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
    page.wait_for_selector("body")
    cookies = context.cookies()
    final_cookies = {}
    expiry = None
    for i in cookies:
        if i.get("name") == "aws-waf-token":
            expiry = i["expires"]
        final_cookies[i["name"]] = i["value"]
    final_cookies.update(
        {
            "ds_cookie_preference": "%257B%2522level%2522%253A%2522all%2522%257D",
        }
    )
    if not expiry:
        print(
            "Please check your internet or deepseek might be blocked in your region. As we got cookies:",
            cookies,
        )

    with open("aws_cookies_deepseek.json", "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))
    print("Cookies saved successfully in file aws_cookies_deepseek.json!")
    print(
        "Keep in mind that these cookies would expire on",
        datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M:%S"),
        ". After this time you would again need to generate cookies as they would expire.",
    )
    print("If you gets rate limit then also sometimes refreshing these cookies works.")
