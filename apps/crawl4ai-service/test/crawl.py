import requests
import time
import csv
import json
import os

API_URL = "http://localhost:3002/v2/crawl"
API_KEY = "b61bc0aa-fea8-485d-b723-662229a047f1"
RESULTS_DIR = "./results"
CSV_FILE = "urls.csv"
TIMEOUT_SECONDS = 600  # 10 minutes
POLL_INTERVAL = 5      # seconds between status checks

os.makedirs(RESULTS_DIR, exist_ok=True)

def crawl_url(slug, url):
    """Submit a crawl job for the given URL."""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    payload = {
        "url": url,
        "excludePaths": [
            "^/wp-content/plugins/wp-event-solution/core/calendar/iCalender/.",
            "^.\.ics$",
            "^.download-ics\.php.",
            "^webcal://.",
            "^/events(?:/.)?",
            "outlook-ical",
            "ical=1",
            "^/blog(?:/.)?",
            "^/jobs?(?:/.)?",
            "^/careers?(?:/.)?",
            "^/news(?:/.)?",
            "^/press(?:/.)?",
            "^/category(?:/.)?",
            "^/tag(?:/.*)?"
        ]
    }

    print(f"[{slug}] Sending payload: {json.dumps(payload)}")

    try:
        resp = requests.post(API_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        crawl_id = data.get("id")

        if not crawl_id:
            print(f"[{slug}] ❌ No crawl ID returned. Response: {data}")
            return

        print(f"[{slug}] ✅ Crawl job submitted. ID: {crawl_id}")
        result = poll_crawl_status(slug, crawl_id)
        save_result(slug, result)  # Always save result, even if timed out

    except requests.HTTPError as e:
        print(f"[{slug}] HTTP error {e.response.status_code}: {e.response.text}")
    except Exception as e:
        print(f"[{slug}] Error submitting crawl: {e}")

def poll_crawl_status(slug, crawl_id):
    """Poll crawl status until completed or timeout."""
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    status_url = f"http://localhost:3002/v2/crawl/{crawl_id}"
    start_time = time.time()

    last_data = None
    while time.time() - start_time < TIMEOUT_SECONDS:
        try:
            resp = requests.get(status_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            last_data = data

            status = data.get("status", "").lower()
            #print(f"[{slug}] Status: {status}")

            if status == "completed":
                print(f"[{slug}] 🎉 Crawl completed.")
                return data

        except Exception as e:
            print(f"[{slug}] Error checking status: {e}")

        time.sleep(POLL_INTERVAL)

    print(f"[{slug}] ⏰ Timeout after {TIMEOUT_SECONDS} seconds.")
    return last_data  # Return the last received data even if not completed

def save_result(slug, data):
    """Save crawl result to ./results/<slug>.json"""
    if data is None:
        print(f"[{slug}] ⚠️ No data to save.")
        return

    filepath = os.path.join(RESULTS_DIR, f"{slug}.json")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        print(f"[{slug}] 💾 Saved to {filepath}")
    except Exception as e:
        print(f"[{slug}] Error saving result: {e}")

def process_csv(file_path):
    """Read CSV and start crawling for each row."""
    with open(file_path, newline='', encoding='utf-8') as csvfile:
        reader = csv.reader(csvfile)
        for row in reader:
            if len(row) < 2:
                continue
            slug, url = row[0].strip(), row[1].strip()
            if not slug or not url:
                continue
            crawl_url(slug, url)

if __name__ == "__main__":
    process_csv(CSV_FILE)