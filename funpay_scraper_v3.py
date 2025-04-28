import requests
from bs4 import BeautifulSoup
import time
import logging
import re
import os
import json # For handling state file
from urllib.parse import urlparse, parse_qs
import sys

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OFFER_STATE_FILE = "offer_state.json" # Stores {offer_id: last_price}
PRICE_CHANGE_THRESHOLD = 5.00 # Ignore price changes <= this value (in USD)
# INCLUDE_PRICE_INCREASES_IN_MSG = False # Set to True if you want to see price increases too

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
}
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT = 20
TELEGRAM_MAX_MSG_LENGTH = 4096
DESCRIPTION_TRUNCATE_LENGTH = 90 # Max chars for description in message

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Helper Functions ---
# (load_offer_state, save_offer_state, extract_offer_id_from_href, extract_sp_from_description - unchanged)
def load_offer_state(filename):
    """Loads offer state ({offer_id: last_price}) from a JSON file."""
    offer_state = {}
    if not os.path.exists(filename):
        logging.info(f"Offer state file '{filename}' not found. Starting fresh.")
        return offer_state
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            offer_state = json.load(f)
        validated_state = {}
        for offer_id, price in offer_state.items():
             if isinstance(price, (int, float)):
                 validated_state[str(offer_id)] = float(price)
             else:
                 logging.warning(f"Invalid price type '{type(price)}' for ID {offer_id} in state file. Skipping.")
        logging.info(f"Loaded state for {len(validated_state)} offers from '{filename}'.")
        return validated_state
    except Exception as e:
        logging.error(f"Error loading state from '{filename}': {e}. Starting with empty state.")
    return {}

def save_offer_state(filename, current_state):
    """Saves the current offer state ({offer_id: current_price}) to a JSON file."""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(current_state, f, indent=2, ensure_ascii=False)
        logging.info(f"Successfully saved state for {len(current_state)} offers to '{filename}'.")
    except Exception as e:
        logging.error(f"Error writing state file '{filename}': {e}")

def extract_offer_id_from_href(href):
    """Extracts the offer ID from the 'id' query parameter of a URL."""
    if not href: return None
    try:
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        offer_id = query_params.get('id', [None])[0]
        if offer_id and offer_id.isdigit(): return str(offer_id) # Return as string
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
            try: return float(match.group(1))
            except (ValueError, IndexError): continue
    return None

def format_offer_for_message(offer_details):
    """Formats the details of a single offer for the Telegram message."""
    # (Unchanged)
    desc = ' '.join(offer_details['description'].split())
    if len(desc) > DESCRIPTION_TRUNCATE_LENGTH:
        desc = desc[:DESCRIPTION_TRUNCATE_LENGTH] + "..."
    else:
        desc = desc
    price_str = f"${offer_details['price_usd']:.2f}" if offer_details['price_usd'] is not None else offer_details['price_text']
    sp_str = f"{offer_details['sp_million']:.1f}M SP" if offer_details['sp_million'] is not None else "SP N/A"
    link = offer_details.get('href', f"https://funpay.com/en/lots/offer?id={offer_details['id']}")
    lines = [
        f"Desc: {desc}",
        f"Price: {price_str}",
        f"SP: {sp_str}",
        f"Link: {link}"
    ]
    return "\n".join(lines)

# --- Core Scraping Function ---
def scrape_all_offers_details(url):
    """Scrapes ALL offers, returning dict {id: details} or None on failure."""
    # (Unchanged)
    logging.info(f"Attempting to fetch and parse ALL offers from: {url}")
    all_offers_details = {}
    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds...")
        time.sleep(REQUEST_DELAY_SECONDS)
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        logging.info(f"Response status code: {response.status_code}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers.")
        if not offer_containers: return {}

        for container in offer_containers:
            href = container.get('href')
            offer_id = extract_offer_id_from_href(href)
            if not offer_id: continue

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

            all_offers_details[offer_id] = {
                'id': offer_id, 'description': description, 'seller': seller,
                'price_usd': price_usd, 'price_text': price_text,
                'sp_million': extracted_sp, 'href': href
            }
        logging.info(f"Successfully extracted details for {len(all_offers_details)} offers.")
        return all_offers_details
    except requests.exceptions.RequestException as e:
        logging.error(f"Network/Request Error during scraping: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during scraping: {e}")
    return None

# --- Telegram Notification Function ---
def send_telegram_notification(bot_token, chat_id, message_text):
    """Sends the provided message text to Telegram."""
    # (Unchanged)
    if not message_text:
        logging.info("No message content provided to send notification.")
        return False
    if not bot_token or not chat_id:
        logging.error("Telegram Bot Token or Chat ID is missing.")
        return False
    if len(message_text.encode('utf-8')) > TELEGRAM_MAX_MSG_LENGTH:
        logging.warning(f"Message length exceeds limit ({TELEGRAM_MAX_MSG_LENGTH} bytes). Truncating.")
        message_bytes = message_text.encode('utf-8')
        message_bytes = message_bytes[:TELEGRAM_MAX_MSG_LENGTH - 20]
        message_text = message_bytes.decode('utf-8', 'ignore') + "\n... (message truncated)"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    params = {'chat_id': chat_id, 'text': message_text, 'disable_web_page_preview': 'true'}
    try:
        logging.info(f"Sending notification to Telegram chat ID ending in ...{chat_id[-4:]}")
        response = requests.post(url, data=params, timeout=15)
        response.raise_for_status()
        response_data = response.json()
        if response_data.get("ok"):
            logging.info("Telegram notification sent successfully.")
            return True
        else:
            error_desc = response_data.get("description", "Unknown error")
            logging.error(f"Telegram API Error: {error_desc}")
            return False
    except Exception as e:
        logging.error(f"Error sending Telegram notification: {e}")
        if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
            logging.error(f"Response status: {e.response.status_code}")
            logging.error(f"Response text: {e.response.text[:200]}...")
        return False

# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time = time.time()
    logging.info("="*30)
    logging.info("Starting Funpay scraper script - Tracking New Offers & Price Changes")

    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("FATAL: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set.")
        sys.exit("Exiting: Missing Telegram credentials.")

    # 1. Load previous offer state ({id: price})
    previous_offer_state = load_offer_state(OFFER_STATE_FILE)

    # 2. Scrape current offers and their details
    current_offers_details = scrape_all_offers_details(URL)

    if current_offers_details is None:
        logging.error("Scraping failed. Exiting.")
        sys.exit("Exiting: Scraping function failed.")

    # 3. Compare current offers with previous state
    new_offers = []
    price_decreased = []
    price_increased = []
    next_offer_state = {}

    for offer_id, current_details in current_offers_details.items():
        current_price = current_details.get('price_usd')
        if current_price is not None:
            next_offer_state[offer_id] = current_price

        if offer_id not in previous_offer_state:
            logging.info(f"-> New Offer ID: {offer_id}")
            new_offers.append(current_details)
        else:
            last_price = previous_offer_state.get(offer_id)
            if current_price is not None and last_price is not None:
                price_diff = abs(current_price - last_price)
                if price_diff > PRICE_CHANGE_THRESHOLD:
                    if current_price < last_price:
                        logging.info(f"-> Price Decrease ID: {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Diff: ${price_diff:.2f})")
                        current_details['last_price'] = last_price
                        price_decreased.append(current_details)
                    elif current_price > last_price:
                         logging.info(f"-> Price Increase ID: {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Diff: ${price_diff:.2f})")
                         current_details['last_price'] = last_price
                         price_increased.append(current_details)


    # --- >>> ADD SORTING LOGIC HERE <<< ---
    # Sort each list by 'price_usd'. Use float('inf') for None prices to put them last.
    price_sort_key = lambda item: item.get('price_usd', float('inf'))
    new_offers.sort(key=price_sort_key)
    price_decreased.sort(key=price_sort_key)
    price_increased.sort(key=price_sort_key)
    logging.info("Sorted offer lists by price (ascending, None last).")
    # --- End of Sorting Logic ---


    # 4. Format and Send Notification (if anything changed)
    notification_needed = bool(new_offers or price_decreased or price_increased)
    notification_sent = False

    if notification_needed:
        logging.info("Changes detected, preparing notification message.")
        message_parts = ["FunPay(EVE ECHOES) Update:\n"]
        item_counter = 0 # Use a single counter for all items

        if new_offers:
            message_parts.append("✨ New Offers:")
            message_parts.append("-" * 15) # Separator
            for offer in new_offers: # Iterate sorted list
                item_counter += 1
                formatted_offer = format_offer_for_message(offer)
                message_parts.append(f"#{item_counter}\n{formatted_offer}")
                message_parts.append("") # Blank line between offers
            if message_parts[-1] == "": message_parts.pop()
            message_parts.append("=" * 15) # Section end

        if price_decreased:
            message_parts.append("\n💲⬇️")
            message_parts.append("-" * 10)
            for offer in price_decreased: # Iterate sorted list
                item_counter += 1
                formatted_offer = format_offer_for_message(offer)
                price_change_line = f"Price (⬇️): ${offer['last_price']:.2f} -> ${offer['price_usd']:.2f} "
                message_parts.append(f"#{item_counter}\n{price_change_line}\n{formatted_offer}")
                message_parts.append("")
            if message_parts[-1] == "": message_parts.pop()
            message_parts.append("=" * 15)

        if price_increased: # Check flag if you add it back
            message_parts.append("\n💲⬆️")
            message_parts.append("-" * 10)
            for offer in price_increased: # Iterate sorted list
                item_counter += 1
                formatted_offer = format_offer_for_message(offer)
                price_change_line = f"Price (⬆️): ${offer['last_price']:.2f} -> ${offer['price_usd']:.2f}"
                message_parts.append(f"#{item_counter}\n{price_change_line}\n{formatted_offer}")
                message_parts.append("")
            if message_parts[-1] == "": message_parts.pop()
            message_parts.append("*" * 15)

        full_message = "\n".join(message_parts)

        notification_sent = send_telegram_notification(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            full_message
        )
    else:
        logging.info("No new offers or significant price changes detected.")

    # 5. Save the *current* state to the file for the next run
    save_offer_state(OFFER_STATE_FILE, next_offer_state)

    end_time = time.time()
    logging.info(f"Script finished in {end_time - start_time:.2f} seconds.")
    logging.info("="*30)
