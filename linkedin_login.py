#!/usr/bin/env python3
"""
One-time LinkedIn login setup.

Launches Chrome with a dedicated scraper profile and opens the LinkedIn
login page. Log in manually, then press Enter to save the session.
Future scraper runs will reuse this session automatically.
"""
import subprocess
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import config

DATA_DIR = config.LINKEDIN_CHROME_DATA_DIR

print("Launching Chrome...")
proc = subprocess.Popen([
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "--remote-debugging-port=9222",
    f"--user-data-dir={DATA_DIR}",
    "--no-first-run",
    "--no-default-browser-check",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

time.sleep(5)

opts = Options()
opts.debugger_address = "127.0.0.1:9222"
driver = webdriver.Chrome(options=opts)

driver.get("https://www.linkedin.com/login")
print()
print("==> Log into LinkedIn in the browser window.")
print("==> Once you see your feed, press Enter here.")
input()

print(f"Title: {driver.title}")
print(f"Session saved to {DATA_DIR}")
print("You can now run: python main.py")

driver.quit()
proc.terminate()
