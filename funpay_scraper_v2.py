import requests
from bs4 import BeautifulSoup
import time
import logging
import re
import os
from urllib.parse import urlparse, parse_qs
import sys # For exiting on error

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
# OFFERS_OUTPUT_FILE = "offers.txt" # No longer needed
PROCESSED_IDS_FILE = "processed_ids.txt" # File to store IDs already notified
# MAX_PRICE_USD = 50.00 # Filtering criteria removed from core logic
# MIN_SP_MILLION = 10.0 # Filtering criteria removed from core logic

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
}
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT = 20
TELEGRAM_MAX_MSG_LENGTH = 4096 # Telegram message length limit

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper Functions ---

def load_processed_ids(filename):
    """Loads previously processed offer IDs from a file into a set."""
    processed_ids = set()
    if not os.path.exists(filename):
        logging.info(f"Processed IDs file '{filename}' not found.")
        return processed_ids
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and line.isdigit():
                    processed_ids.add(line)
                elif line:
                    logging.warning(f"Skipping non-digit line in '{filename}': '{line}'")
        logging.info(f"Loaded {len(processed_ids)} processed IDs from '{filename}'.")
    except IOError as e:
        logging.error(f"Error reading processed IDs file '{filename}': {e}")
        return set()
    except Exception as e:
        logging.error(f"Unexpected error loading IDs from '{filename}': {e}")
        return set()
    return processed_ids

def append_processed_ids(filename, new_ids):
    """Appends newly processed offer IDs to the file."""
    if not new_ids:
        return
    try:
        with open(filename, 'a', encoding='utf-8') as f:
            for offer_id in new_ids:
                f.write(f"{offer_id}\n")
        logging.info(f"Appended {len(new_ids)} new IDs to '{filename}'.")
    except IOError as e:
        logging.error(f"Error appending to processed IDs file '{filename}': {e}")
    except Exception as e:
        logging.error(f"Unexpected error appending IDs to '{filename}': {e}")

def extract_offer_id_from_href(href):
    """Extracts the offer ID from the 'id' query parameter of a URL."""
    if not href: return None
    try:
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        offer_id = query_params.get('id', [None])[0]
        if offer_id and offer_id.isdigit(): return offer_id
    except Exception as e:
        logging.warning(f"Could not parse offer ID from href '{href}': {e}")
    return None

def extract_sp_from_description(description):
    """Attempts to extract Skill Points (in millions) from the description text."""
    description_lower = description.lower()
    patterns = [
        r'(\d+(?:\.\d+)?)\s*(?:m|mil|million)\s*sp',
        r'sp\s*(\d+(?:\.\d+)?)\s*(?:m|mil|million)',
        r'(\d+(?:\.\d+)?)\s*sp'
    ]
    for pattern in patterns:
        match = re.search(pattern, description_lower)
        if match:
            potential_k_context = description_lower[max(0, match.start()-5):min(len(description_lower), match.end()+5)]
            is_thousand = False
            if 'k sp' in potential_k_context or 'k ' in potential_k_context: is_thousand = True
            if match.start(1) > 0 and description_lower[match.start(1)-1:match.start(1)] == 'k': is_thousand = True
            if is_thousand: continue
            try:
                return float(match.group(1))
            except (ValueError, IndexError): continue
    return None # Return None if no valid SP value found


# --- Core Scraping Function ---

def scrape_all_offers_details(url):
    """
    Scrapes ALL offers from the page and returns a dictionary mapping
    offer_id to its details. Returns None on failure.
    """
    logging.info(f"Attempting to fetch and parse ALL offers from: {url}")
    all_offers_details = {} # Store details keyed by offer ID

    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds...")
        time.sleep(REQUEST_DELAY_SECONDS)
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        logging.info(f"Response status code: {response.status_code}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers.")

        if not offer_containers: return {} # Return empty dict if none found

        count = 0
        for container in offer_containers:
            count += 1
            href = container.get('href')
            offer_id = extract_offer_id_from_href(href)
            if not offer_id:
                logging.warning(f"Container #{count}: Could not extract valid Offer ID. Skipping.")
                continue

            desc_tag = container.find('div', class_='tc-desc-text')
            description = desc_tag.get_text(separator=' ', strip=True) if desc_tag else "N/A"

            seller_tag = container.find('div', class_='media-user-name')
            seller = seller_tag.get_text(strip=True) if seller_tag else "N/A"

            price_container_tag = container.find('div', class_='tc-price')
            price_text = price_container_tag.get_text(strip=True) if price_container_tag else "N/A"
            price_usd = None
            if price_text != "N/A":
                try:
                    price_match = re.search(r'[\$€£]?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)', price_text.replace(',', ''))
                    if price_match: price_usd = float(price_match.group(1))
                    else:
                        fallback_match = re.search(r'(\d+\.?\d*)', price_text)
                        if fallback_match: price_usd = float(fallback_match.group(1))
                except ValueError:
                    logging.warning(f"Offer ID {offer_id}: Could not parse price '{price_text}'")

            extracted_sp = extract_sp_from_description(description)

            # Store details for this offer ID
            all_offers_details[offer_id] = {
                'id': offer_id,
                'description': description,
                'seller': seller,
                'price_usd': price_usd, # Can be None if parsing failed
                'price_text': price_text,
                'sp_million': extracted_sp, # Can be None
                'href': href if href else f"https://funpay.com/en/lots/offer?id={offer_id}"
            }
        logging.info(f"Successfully extracted details for {len(all_offers_details)} offers.")
        return all_offers_details

    except requests.exceptions.RequestException as e:
        logging.error(f"Network/Request Error during scraping: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during scraping: {e}")

    return None # Indicate failure


# --- Telegram Notification Function ---

def send_telegram_notification(bot_token, chat_id, new_offers_details_list):
    """Formats and sends a notification about new offers to Telegram."""
    if not new_offers_details_list:
        logging.info("No new offers provided to send notification.")
        return False # Nothing to send

    if not bot_token or not chat_id:
        logging.error("Telegram Bot Token or Chat ID is missing. Cannot send notification.")
        return False

    logging.info(f"Preparing Telegram notification for {len(new_offers_details_list)} new offers.")

    # Format the message content
    message_lines = [f"🚨 {len(new_offers_details_list)} New FunPay Offers Found! 🚨\n"]

    for i, offer in enumerate(new_offers_details_list):
        message_lines.append(f"#{i+1} (ID: {offer['id']})")
        desc = ' '.join(offer['description'].split())[:150] # Limit description length
        message_lines.append(f"  Desc: {desc}{'...' if len(offer['description']) > 150 else ''}")
        message_lines.append(f"  Seller: {offer['seller']}")
        price_str = f"${offer['price_usd']:.2f}" if offer['price_usd'] is not None else offer['price_text']
        message_lines.append(f"  Price: {price_str}")
        sp_str = f"{offer['sp_million']:.1f}M SP" if offer['sp_million'] is not None else "SP N/A"
        message_lines.append(f"  SP: {sp_str}")
        message_lines.append(f"  Link: {offer['href']}")
        message_lines.append("-" * 20)

    full_message = "\n".join(message_lines)

    # Truncate if message exceeds Telegram limit
    if len(full_message.encode('utf-8')) > TELEGRAM_MAX_MSG_LENGTH:
        logging.warning(f"Message length exceeds limit ({TELEGRAM_MAX_MSG_LENGTH} bytes). Truncating.")
        # Truncate based on bytes, trying to preserve UTF-8
        message_bytes = full_message.encode('utf-8')
        message_bytes = message_bytes[:TELEGRAM_MAX_MSG_LENGTH - 20] # Leave space for ellipsis etc.
        full_message = message_bytes.decode('utf-8', 'ignore') + "\n... (message truncated)"

    # Prepare API request
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    params = {
        'chat_id': chat_id,
        'text': full_message,
        'disable_web_page_preview': 'true' # Optional: disable link previews
    }

    # Send the request
    try:
        logging.info(f"Sending notification to Telegram chat ID ending in ...{chat_id[-4:]}")
        response = requests.post(url, data=params, timeout=15)
        response.raise_for_status() # Check for HTTP errors (4xx or 5xx)

        # Check response content for potential Telegram API errors
        response_data = response.json()
        if response_data.get("ok"):
            logging.info("Telegram notification sent successfully.")
            return True
        else:
            error_desc = response_data.get("description", "Unknown error")
            error_code = response_data.get("error_code", "N/A")
            logging.error(f"Telegram API Error: Code {error_code} - {error_desc}")
            return False

    except requests.exceptions.Timeout:
        logging.error("Telegram request timed out.")
        return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Error sending Telegram notification: {e}")
        if e.response is not None:
            logging.error(f"Response status: {e.response.status_code}")
            logging.error(f"Response text: {e.response.text[:200]}...") # Log part of response
        return False
    except Exception as e:
        logging.error(f"Unexpected error during Telegram notification: {e}")
        return False

# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time = time.time()
    logging.info("="*30)
    logging.info("Starting Funpay scraper script - Tracking ALL New Offers")

    # --- Retrieve secrets from environment variables ---
    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("FATAL: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable not set.")
        sys.exit("Exiting: Missing Telegram credentials.") # Exit if secrets aren't present

    # 1. Load previously processed IDs
    processed_ids_set = load_processed_ids(PROCESSED_IDS_FILE)

    # 2. Scrape ALL offers and their details
    # Returns dict {id: details} or None on failure
    all_scraped_offers_details = scrape_all_offers_details(URL)

    if all_scraped_offers_details is None:
        logging.error("Scraping failed. Exiting.")
        sys.exit("Exiting: Scraping function failed.")

    # 3. Identify NEW offers
    current_scraped_ids = set(all_scraped_offers_details.keys())
    new_offer_ids = current_scraped_ids - processed_ids_set # Set difference

    if new_offer_ids:
        logging.info(f"Found {len(new_offer_ids)} NEW offer IDs: {', '.join(sorted(list(new_offer_ids)))}")

        # Prepare list of details ONLY for the new offers
        new_offers_for_notification = [all_scraped_offers_details[id] for id in new_offer_ids if id in all_scraped_offers_details]

        # 4. Send Notification for NEW offers
        notification_sent = send_telegram_notification(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            new_offers_for_notification
        )

        # 5. Update processed IDs file ONLY if notification was successful
        if notification_sent:
            logging.info("Notification successful. Updating processed IDs file.")
            append_processed_ids(PROCESSED_IDS_FILE, new_offer_ids)
        else:
            logging.error("Telegram notification failed. Processed IDs file will NOT be updated to ensure retry on next run.")

    else:
        logging.info("No new offer IDs found compared to the processed list.")

    end_time = time.time()
    logging.info(f"Script finished in {end_time - start_time:.2f} seconds.")
    logging.info("="*30)
