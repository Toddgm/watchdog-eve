# --- START OF FILE funpay_scraper_final.py ---

import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime,timedelta
import logging
import re
import os
import json # For handling state file
from urllib.parse import urlparse, parse_qs
import sys

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from dotenv import load_dotenv
    if os.path.exists('.env'):
        load_dotenv()
        logging.info("Loaded environment variables from .env file.")
except ImportError:
    logging.info("python-dotenv not installed, skipping .env file loading.")
    pass # Ignore if dotenv is not installed

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OFFER_STATE_FILE = "offer_state.json" # Stores {offer_id: last_price}
PRICE_CHANGE_PERCENT_THRESHOLD = 5.0 # Notify if price decreases by more than this percentage

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
}
# Add cookie to force USD currency display
COOKIES = {
    'cy': 'USD'
}
REQUEST_DELAY_SECONDS = 1
REQUEST_TIMEOUT = 20
TELEGRAM_MAX_MSG_LENGTH = 4096
DISCORD_MAX_MSG_LENGTH = 2000 # Discord message length limit
DISCORD_SEND_DELAY_SECONDS = 1 # Delay between sending message chunks
DESCRIPTION_TRUNCATE_LENGTH = 90 # Max chars for description in message

# Environment variable names
TELEGRAM_BOT_TOKEN_ENV = 'TELEGRAM_BOT_TOKEN'
TELEGRAM_CHAT_ID_ENV = 'TELEGRAM_CHAT_ID'
DISCORD_WEBHOOK_URL = 'DISCORD_WEBHOOK_URL' # Single Discord webhook


# --- Helper Functions ---
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
        # Ensure IDs are strings and prices are floats or None
        for offer_id, price in offer_state.items():
             if isinstance(price, (int, float, type(None))):
                 # Convert int/float to float, keep None as None
                 validated_state[str(offer_id)] = float(price) if isinstance(price, (int, float)) else None
             else:
                 logging.warning(f"Invalid price type '{type(price)}' for ID {offer_id} in state file '{filename}'. Skipping.")
        logging.info(f"Loaded state for {len(validated_state)} offers from '{filename}'.")
        return validated_state
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from '{filename}': {e}. Starting with empty state.")
    except Exception as e:
        logging.error(f"Error loading state from '{filename}': {e}. Starting with empty state.")
    return {}

def save_offer_state(filename, current_state):
    """Saves the current offer state ({offer_id: current_price}) to a JSON file."""
    state_to_save = {}
    for offer_id, price in current_state.items():
         # Only save if price is a valid type (float or None)
         if isinstance(price, (float, type(None))): # Use float as int prices are converted to float on load/process
             state_to_save[str(offer_id)] = price
         else:
             logging.warning(f"Attempted to save invalid price type '{type(price)}' for ID {offer_id}. Skipping.")

    try:
        # Sort keys for consistent file output (optional but helpful for diffs)
        sorted_state_to_save = dict(sorted(state_to_save.items(), key=lambda item: int(item[0])))
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(sorted_state_to_save, f, indent=2, ensure_ascii=False)
        logging.info(f"Successfully saved state for {len(state_to_save)} offers to '{filename}'.")
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
    """
    Attempts to extract Skill Points (in millions) strictly from the end of the description.
    Includes a sanity check for very large numbers.
    """
    if not description: return None
    # Remove commas and leading/trailing whitespace for consistent processing
    description_lower = description.lower().replace(',', '').strip()

    # Pattern: Looks for optional leading space/comma, then number, strictly " m sp", optional trailing space, END
    strict_end_pattern = r'[\s,]*(\d+(?:\.\d+)?)\s*m\s*sp\s*$'
    match = re.search(strict_end_pattern, description_lower)

    if match:
        sp_value_str = match.group(1)
        try:
            sp_value = float(sp_value_str)
            SANITY_CHECK_THRESHOLD = 1000 # If extracted number is > 1000, it's probably not M SP
            if sp_value > SANITY_CHECK_THRESHOLD:
                 corrected_sp_value = sp_value / 1_000_000.0 # Divide by one million
                 logging.warning(
                     f"Abnormal SP value '{sp_value_str}'. "
                     f"Converting to {corrected_sp_value:.2f}M SP."
                 )
                 return corrected_sp_value
            return sp_value
        except ValueError:
            logging.warning(f"Could not convert extracted strict end-SP value '{sp_value_str}' to float.")
            return None
    else:
        logging.debug(f"Strict 'X m SP' pattern not found at end of description: '{description}'")
        return None

def format_offer_body(offer_details):
    """Formats the core details of a single offer (description, price, sp, link) for the message."""
    desc = ' '.join(offer_details['description'].split())
    if len(desc) > DESCRIPTION_TRUNCATE_LENGTH:
        sp_match = re.search(r'\d+(?:\.\d+)?\s*m\s*sp\s*$', desc.lower())
        if sp_match:
             keep_length = len(desc) - sp_match.start()
             if keep_length < DESCRIPTION_TRUNCATE_LENGTH:
                  start_index = max(0, len(desc) - DESCRIPTION_TRUNCATE_LENGTH + 3) # +3 for "..."
                  desc = "..." + desc[start_index:]
             else:
                 desc = "..." + desc[sp_match.start():]
        else:
             desc = desc[:DESCRIPTION_TRUNCATE_LENGTH] + "..."

    price_str = f"${offer_details['price_usd']:.2f}" if offer_details['price_usd'] is not None else offer_details.get('price_text', 'Price N/A')
    sp_str = f"{offer_details['sp_million']:.1f}mil" if offer_details['sp_million'] is not None else "SP N/A"
    link = offer_details.get('href', f"https://funpay.com/en/lots/offer?id={offer_details['id']}")

    lines = [
        f"Desc: {desc}",
        f"Price: {price_str}",
        f"SP: {sp_str}",
        f"Link: {link}"
    ]
    return "\n".join(lines)

def format_offer_block_lines(offer_details, item_number):
    """Formats a complete block of text for a single offer including header, ratio, and body."""
    price_usd = offer_details.get('price_usd')
    sp_million = offer_details.get('sp_million')
    discount_percent = offer_details.get('discount_percent') # Get discount % if available
    ratio_str = ""

    if price_usd is not None and sp_million is not None and sp_million > 0:
        try:
            price_per_million = price_usd / sp_million
            ratio_str = f" [${price_per_million:.2f}/mil]"
        except Exception as e:
             logging.warning(f"Could not calculate price/SP ratio for offer {offer_details.get('id', 'N/A')}: {e}")

    header_line = f"#{item_number}"
    header_line += f"{ratio_str}\n"
    if discount_percent is not None and offer_details.get('last_price') is not None and price_usd is not None:
        last_price = offer_details['last_price']
        header_line += f" (${last_price:.2f} -> ${price_usd:.2f})" # Show price change

    if discount_percent is not None:
         header_line += f" ⬇ (-{discount_percent:.1f}%)" # Add formatted discount


    offer_body = format_offer_body(offer_details)
    return [header_line, offer_body, ""]

def append_offer_section(message_parts_list, current_item_counter, offer_list, section_title, section_separator):
    """Appends a formatted section of offers to the message parts list. Returns the updated item counter."""
    if offer_list:
        message_parts_list.append(f"\n{section_title}")
        message_parts_list.append(section_separator)
        item_counter = current_item_counter
        for offer in offer_list:
            item_counter += 1
            offer_block_lines = format_offer_block_lines(offer, item_counter)
            message_parts_list.extend(offer_block_lines)
        if message_parts_list and message_parts_list[-1] == "":
            message_parts_list.pop()
        message_parts_list.append("=" * 15)
        return item_counter
    return current_item_counter

# --- Core Scraping Function ---
def scrape_all_offers_details(url):
    """Scrapes ALL offers, returning dict {id: details} or None on failure."""
    logging.info(f"Fetch and parse ALL offers from: {url}")
    all_offers_details = {}
    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds...")
        time.sleep(REQUEST_DELAY_SECONDS)
        logging.info("Adding cookie 'cy=USD' to request.")
        response = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=REQUEST_TIMEOUT)
        logging.info(f"Response status code: {response.status_code}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer(s).")
        if not offer_containers:
            logging.warning("No offer containers found on the page.")
            return {}

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
            cleaned_price_text = price_text
            for symbol in ['$', '€', '£', '₽']:
                 cleaned_price_text = cleaned_price_text.replace(symbol, '')
            cleaned_price_text = cleaned_price_text.replace(' ', '').replace(',', '')
            try:
                price_match = re.search(r'(\d+\.?\d*)', cleaned_price_text)
                if price_match:
                     price_usd = float(price_match.group(1))
            except ValueError:
                 logging.warning(f"Offer ID {offer_id}: Could not convert price '{price_match.group(1) if price_match else cleaned_price_text}' from '{price_text}'")
            except Exception as e:
                 logging.warning(f"Offer ID {offer_id}: Error parsing price '{price_text}': {e}")

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
        logging.error(f"Unexpected error during scraping: {e}", exc_info=True) # Include traceback
    return None

# --- Notification Functions ---
def send_telegram_notification(bot_token, chat_id, message_text):
    """Sends the provided message text to Telegram."""
    if not message_text:
        logging.info("No message content provided to send Telegram notification.")
        return False
    if not bot_token or not chat_id:
        logging.error("Telegram Bot Token or Chat ID is missing.")
        return False
    message_bytes = message_text.encode('utf-8')
    if len(message_bytes) > TELEGRAM_MAX_MSG_LENGTH:
        logging.warning(f"Telegram message length exceeds limit ({TELEGRAM_MAX_MSG_LENGTH} bytes). Truncating.")
        truncated_bytes = message_bytes[:TELEGRAM_MAX_MSG_LENGTH - 30].decode('utf-8', 'ignore').encode('utf-8')
        message_text = truncated_bytes.decode('utf-8', 'ignore') + "\n... (truncated)"

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
            logging.error(f"Attempted message start: {message_text[:200]}...")
            return False
    except Exception as e:
        logging.error(f"Error sending Telegram notification: {e}")
        if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
            logging.error(f"Telegram Response status: {e.response.status_code}, Text: {e.response.text[:200]}...")
        return False

def send_discord_notification(webhook_url, message_text):
    """Sends the provided message text to Discord via webhook, splitting if necessary."""
    if not message_text:
        logging.info("No message content provided to send Discord notification.")
        return False
    if not webhook_url:
        logging.error("Discord Webhook URL is missing.")
        return False

    headers = {'Content-Type': 'application/json'}
    remaining_message = message_text
    success = True
    message_index = 0

    while len(remaining_message) > 0:
        message_index += 1
        chunk_to_send = ""
        # Determine the chunk to send
        if len(remaining_message) <= DISCORD_MAX_MSG_LENGTH:
            chunk_to_send = remaining_message
            remaining_message = "" # Last chunk
        else:
            # Find the best place to split (last newline before the limit)
            split_point = remaining_message.rfind('\n', 0, DISCORD_MAX_MSG_LENGTH)
            if split_point == -1: # No newline found, split hard
                split_point = DISCORD_MAX_MSG_LENGTH
            chunk_to_send = remaining_message[:split_point]
            remaining_message = remaining_message[split_point:].lstrip()

        # Prepare payload
        # Add chunk indicator if message is split
        if message_index > 1:
             chunk_to_send = f"(Part {message_index}) ...\n{chunk_to_send}"
        if len(remaining_message) > 0 :
             chunk_to_send += f"\n... (Continued in next part)"

        payload = json.dumps({'content': chunk_to_send})

        try:
            logging.info(f"Sending chunk {message_index} to Discord webhook (approx {len(chunk_to_send)} chars)...")
            response = requests.post(webhook_url, headers=headers, data=payload, timeout=15)
            response.raise_for_status()
            logging.info(f"Discord chunk {message_index} sent successfully (Status: {response.status_code}).")
            if len(remaining_message) > 0:
                logging.debug(f"Waiting {DISCORD_SEND_DELAY_SECONDS}s before next Discord chunk...")
                time.sleep(DISCORD_SEND_DELAY_SECONDS)
        except Exception as e:
            logging.error(f"Error sending Discord notification chunk {message_index}: {e}")
            if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
                logging.error(f"Discord Response status: {e.response.status_code}, Text: {e.response.text[:200]}...")
            success = False
            break # Stop trying

    if success and message_index > 1:
        logging.info("All Discord message chunks sent successfully.")
    elif success:
         logging.info("Discord notification sent successfully (single chunk).")
    else:
         logging.error("Failed to send full message to Discord.")
    return success


# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time = time.time()
    utc8_time = datetime.utcfromtimestamp(start_time) + timedelta(hours=8)
    timestamp = utc8_time.strftime('%Y-%m-%d %H:%M:%S')
    logging.info("="*30)
    logging.info("Starting Funpay scraper script - Tracking New Offers & Discounts")

    # Get Credentials from Environment Variables
    TELEGRAM_BOT_TOKEN = os.environ.get(TELEGRAM_BOT_TOKEN_ENV)
    TELEGRAM_CHAT_ID = os.environ.get(TELEGRAM_CHAT_ID_ENV)
    DISCORD_WEBHOOK_URL = os.environ.get(DISCORD_WEBHOOK_URL)

    # Validate Credentials
    notify_via_telegram = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    notify_via_discord = bool(DISCORD_WEBHOOK_URL)

    if not notify_via_telegram and not notify_via_discord:
         logging.error("FATAL: No notification credentials (Telegram or Discord) found. Set environment variables.")
         sys.exit("Exiting: Missing notification credentials.")
    if not notify_via_telegram:
         logging.warning("Telegram credentials missing. Will only notify via Discord if configured.")
    if not notify_via_discord:
         logging.warning(f"{DISCORD_WEBHOOK_URL} env var not set. Will only notify via Telegram if configured.")


    # 1. Load previous offer state ({id: price})
    previous_offer_state = load_offer_state(OFFER_STATE_FILE)

    # 2. Scrape current offers and their details
    current_offers_details = scrape_all_offers_details(URL)

    if current_offers_details is None:
        logging.error("Scraping failed. Exiting.")
        sys.exit("Exiting: Scraping function failed.")

    # 3. Compare current offers with previous state; Build notification lists
    new_offers = []
    discounted_offers = []
    offers_for_next_state = {} # Holds data for the state file IF we decide to save

    logging.info("Comparing current offers to previous state...")
    current_offer_ids = set()
    for offer_id, current_details in current_offers_details.items():
        current_offer_ids.add(offer_id)
        current_price = current_details.get('price_usd')
        last_price = previous_offer_state.get(offer_id)

        # Tentatively add all current offers to the potential next state
        offers_for_next_state[offer_id] = current_price

        # Notification Logic
        if offer_id not in previous_offer_state:
            logging.info(f"-> Found New Offer: {offer_id} (Price: ${current_price:.2f})" if current_price is not None else f"-> Found New Offer: {offer_id} (Price: N/A)")
            new_offers.append(current_details)
        else: # Existing offer
            if current_price is not None and last_price is not None and last_price > 0:
                 price_diff = current_price - last_price
                 if price_diff < 0: # Check for decrease only
                      percent_decrease = abs(price_diff / last_price) * 100.0
                      if percent_decrease > PRICE_CHANGE_PERCENT_THRESHOLD:
                           logging.info(f"-> Discount Found (Notify): {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Discount: {percent_decrease:.1f}%)")
                           current_details['last_price'] = last_price
                           current_details['discount_percent'] = percent_decrease
                           discounted_offers.append(current_details)
                      else:
                           logging.info(f"-> Price decrease below threshold for {offer_id} ({percent_decrease:.1f}%). Skip notify.")
            elif current_price is not None and last_price is None:
                  logging.info(f"-> Price now available for {offer_id} (prev N/A): ${current_price:.2f}. Notifying as new.")
                  new_offers.append(current_details)

    # Calculate removed offers count
    previous_offer_ids = set(previous_offer_state.keys())
    removed_offer_ids = previous_offer_ids - current_offer_ids
    removed_offer_count = len(removed_offer_ids)
    if removed_offer_count > 0:
        logging.info(f"Identified {removed_offer_count} offers from previous state not in current scrape: {list(removed_offer_ids)}")

    # 4. Sort the notification lists by price
    price_sort_key = lambda item: item.get('price_usd', float('inf'))
    new_offers.sort(key=price_sort_key)
    discounted_offers.sort(key=price_sort_key)
    logging.info(f"Sorted notification lists: New={len(new_offers)}, Discounts={len(discounted_offers)}, Sold={removed_offer_count}.")

    # 5. Determine if Notification/State Save is Needed and Format Message
    # Notification/Save is needed if there are new offers, discounts, OR removed offers.
    significant_changes_detected = bool(new_offers or discounted_offers)
    state_update_needed = bool(significant_changes_detected or removed_offer_count > 0)

    notification_sent_flags = {'telegram': None, 'discord': None}
    full_message = "" # Initialize message variable

    if state_update_needed: # Check if *any* relevant change occurred (new, discount, or removed)
        logging.info("Relevant changes detected, proceeding with notification/state update logic.")

        if significant_changes_detected:
            # Case 1: New offers or discounts were found (standard message)
            logging.info("Preparing standard notification message (New/Discounts found).")
            message_parts = [f"FunPay(EVE ECHOES) Update:\n{timestamp}"]
            item_counter = 0
            item_counter = append_offer_section(message_parts, item_counter, new_offers, "✨ New Offers:", "-" * 15)
            item_counter = append_offer_section(message_parts, item_counter, discounted_offers, "💰 On Sale:", "-" * 15)
            if removed_offer_count > 0: # Add summary if offers were also removed
                message_parts.append(f"\nAlso, {removed_offer_count} offers were sold/removed since last check.")
            full_message = "\n".join(message_parts)

        elif removed_offer_count > 0:
            # Case 2: ONLY removed offers were found (specific message)
            logging.info("Preparing specific notification message (Only removed offers found).")
            full_message = f"FunPay(EVE ECHOES):\n{timestamp}\nNo new offers or significant discounts found.\n{removed_offer_count} offer(s) were dropped/sold since last check."
            # No need to build detailed sections for this specific message

        # --- Send Notifications (using the composed full_message) ---
        if full_message: # Ensure a message was actually composed
            notification_sent = False # Track if any send succeeds
            if notify_via_discord:
                logging.info("--- Attempting Discord Notification (Primary) ---")
                discord_success = send_discord_notification(DISCORD_WEBHOOK_URL, full_message)
                notification_sent_flags['discord'] = discord_success
                notification_sent_flags['telegram'] = False # Mark Telegram as skipped
                if discord_success: notification_sent = True
                else: logging.error("Discord notification failed.")
            elif notify_via_telegram:
                logging.info("--- Attempting Telegram Notification (Fallback) ---")
                telegram_success = send_telegram_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, full_message)
                notification_sent_flags['telegram'] = telegram_success
                notification_sent_flags['discord'] = False # Mark Discord as skipped
                if telegram_success: notification_sent = True
                else: logging.error("Telegram notification failed.")
            else:
                logging.error("Notification needed but no platform configured!")
        else:
             logging.warning("State update needed, but message composition logic failed.")


        # --- SAVE STATE FILE ---
        # Save the state file because relevant changes were detected
        logging.info(f"Saving state for {len(offers_for_next_state)} currently listed offers because relevant changes were detected.")
        save_offer_state(OFFER_STATE_FILE, offers_for_next_state)
        # --- END SAVE STATE FILE ---

    else:
        # No notification OR state update needed
        logging.info("No new offers, significant discounts, or removed offers detected.")
        logging.info("State file will not be updated.")
        # Note: removed_offer_count must be 0 if we reach here

    # --- Update final status log ---
    end_time = time.time()
    logging.info(f"Script finished in {end_time - start_time:.2f} seconds.")
    # Adjust status logging based on the new combined condition 'state_update_needed'
    if state_update_needed:
        if notify_via_discord:
            tg_status = 'Skipped (Discord Primary)'
            dc_status = f"Attempted (OK={notification_sent_flags['discord']})"
        elif notify_via_telegram:
            tg_status = f"Attempted (OK={notification_sent_flags['telegram']})"
            dc_status = 'Skipped (Telegram Fallback)'
        else:
            tg_status = 'Not Configured'
            dc_status = 'Not Configured'
        logging.info(f"Notification Status: Telegram {tg_status}, Discord {dc_status}")
    logging.info("="*30)

# --- END OF FILE funpay_scraper_final.py ---