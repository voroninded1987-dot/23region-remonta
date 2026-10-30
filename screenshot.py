import sys, time
from playwright.sync_api import sync_playwright
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
url=sys.argv[1]; out=sys.argv[2]
width=int(sys.argv[3]) if len(sys.argv)>3 else 1440
height=int(sys.argv[4]) if len(sys.argv)>4 else 900
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=CHROME, headless=True, args=["--hide-scrollbars","--disable-blink-features=AutomationControlled"])
    page=browser.new_page(viewport={"width":width,"height":height}, device_scale_factor=1)
    errors=[]
    page.on("console", lambda msg: errors.append(f"console {msg.type}: {msg.text}") if msg.type=="error" else None)
    page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
    page.goto(url, wait_until="networkidle", timeout=120000)
    page.wait_for_timeout(3000)
    # scroll through page to trigger lazy loading and animations
    sh=page.evaluate("document.documentElement.scrollHeight")
    y=0
    while y<sh:
        page.evaluate(f"window.scrollTo(0,{y})")
        page.wait_for_timeout(250)
        y+=700
        sh=page.evaluate("document.documentElement.scrollHeight")
    page.evaluate("window.scrollTo(0,0)")
    page.wait_for_timeout(1500)
    page.screenshot(path=out, full_page=True)
    print("TITLE",page.title())
    print("SIZE",page.evaluate("[document.documentElement.scrollWidth,document.documentElement.scrollHeight,innerWidth,innerHeight]"))
    print("ERRORS", len(errors))
    for e in errors[:30]: print(e)
    browser.close()
