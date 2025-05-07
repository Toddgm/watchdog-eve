
import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime,timedelta, timezone
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
OFFER_STATE_FILE = "offer_history_local.json" # CHANGED: Use a new state file for enriched data
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

# --- Helper Functions ---
def load_offer_state(filename):
    """Loads offer state ({offer_id: offer_details_dict}) from a JSON file."""
    offer_state = {}
    # --- CHANGED: Initialize inactive_count ---
    inactive_count = 0
    # --- END CHANGED ---
    if not os.path.exists(filename):
        logging.info(f"Offer state file '{filename}' not found. Starting fresh.")
        # --- CHANGED: Return empty state, no inactive count needed on initial load ---
        return offer_state
        # --- END CHANGED ---
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            loaded_data = json.load(f)

        validated_state = {}
        for offer_id, details in loaded_data.items():
            if isinstance(details, dict):
                price = details.get('price_usd')
                if price is not None and not isinstance(price, (int, float)):
                    logging.warning(f"Invalid price_usd type '{type(price)}' for ID {offer_id} in state file. Setting to None.")
                    details['price_usd'] = None
                elif isinstance(price, (int, float)):
                    details['price_usd'] = float(price)

                details.setdefault('id', str(offer_id))
                details.setdefault('description', "N/A")
                details.setdefault('seller', "N/A")
                details.setdefault('sp_million', None)
                details.setdefault('href', f"https://funpay.com/en/lots/offer?id={offer_id}")
                details.setdefault('last_seen_active', None) # This field isn't used elsewhere, might be vestigial?
                details.setdefault('price_text', "N/A")
                details.setdefault('notified_as_removed_at', None)

                # --- CHANGED: Count inactive offers ---
                if details.get('notified_as_removed_at') is not None:
                     inactive_count += 1
                # --- END CHANGED ---

                validated_state[str(offer_id)] = details
            else:
                logging.warning(f"Invalid data type for ID {offer_id} in state file '{filename}' (expected dict, got {type(details)}). Skipping.")

        # --- CHANGED: Update logging to include inactive count ---
        logging.info(f"Loaded state for {len(validated_state)}({inactive_count}/{len(validated_state)} inactive) offers from '{filename}'.")
        # --- END CHANGED ---

        return validated_state
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from '{filename}': {e}. Starting with empty state.")
    except Exception as e:
        logging.error(f"Error loading state from '{filename}': {e}. Starting with empty state.")
    # --- CHANGED: Return empty state on error ---
    return {}
    # --- END CHANGED ---


def save_offer_state(filename, current_state_dict):
    """Saves the current offer state ({offer_id: offer_details_dict}) to a JSON file."""
    try:
        # Sort by offer_id (as integer for natural sort) for consistent file output
        sorted_state_to_save = dict(sorted(current_state_dict.items(), key=lambda item: int(item[0])))
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(sorted_state_to_save, f, indent=2, ensure_ascii=False)
        logging.info(f"Successfully saved state for {len(sorted_state_to_save)} offers to '{filename}'.")
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
    description_lower = description.lower().replace(',', '').strip()
    strict_end_pattern = r'[\s,]*(\d+(?:\.\d+)?)\s*m\s*sp\s*$'
    match = re.search(strict_end_pattern, description_lower)

    if match:
        sp_value_str = match.group(1)
        try:
            sp_value = float(sp_value_str)
            SANITY_CHECK_THRESHOLD = 1000 
            if sp_value > SANITY_CHECK_THRESHOLD:
                 corrected_sp_value = sp_value / 1_000_000.0
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
    desc = ' '.join(offer_details.get('description', "N/A").split())
    if len(desc) > DESCRIPTION_TRUNCATE_LENGTH:
        sp_match = re.search(r'\d+(?:\.\d+)?\s*m\s*sp\s*$', desc.lower())
        if sp_match:
             keep_length = len(desc) - sp_match.start()
             if keep_length < DESCRIPTION_TRUNCATE_LENGTH:
                  start_index = max(0, len(desc) - DESCRIPTION_TRUNCATE_LENGTH + 3) 
                  desc = "..." + desc[start_index:]
             else:
                 desc = "..." + desc[sp_match.start():]
        else:
             desc = desc[:DESCRIPTION_TRUNCATE_LENGTH] + "..."

    price_usd_val = offer_details.get('price_usd')
    price_str = f"${price_usd_val:.2f}" if price_usd_val is not None else offer_details.get('price_text', 'Price N/A')
    sp_million_val = offer_details.get('sp_million')
    sp_str = f"{sp_million_val:.1f}mil" if sp_million_val is not None else "SP N/A"
    link = offer_details.get('href', f"https://funpay.com/en/lots/offer?id={offer_details.get('id', 'N/A')}")

    lines = [
        f"Desc: {desc}",
        f"Price: {price_str}",
        f"SP: {sp_str}",
        f"Link: {link}"
    ]
    return "\n".join(lines)

def format_offer_block_lines(offer_details, item_number, price_change_prefix=""): # ADDED price_change_prefix
    """Formats a complete block of text for a single offer including header, ratio, and body."""
    price_usd = offer_details.get('price_usd')
    sp_million = offer_details.get('sp_million')
    discount_percent = offer_details.get('discount_percent') 
    ratio_str = ""

    if price_usd is not None and sp_million is not None and sp_million > 0:
        try:
            price_per_million = price_usd / sp_million
            ratio_str = f" [${price_per_million:.2f}/mil]"
        except Exception as e:
             logging.warning(f"Could not calculate price/SP ratio for offer {offer_details.get('id', 'N/A')}: {e}")

    header_line = f"#{item_number}{ratio_str}" # Ratio on the same line as item number

    # Handle price change display for decreased/increased lists
    if price_change_prefix and offer_details.get('last_price') is not None and price_usd is not None:
        last_price = offer_details['last_price']
        header_line += f"\n${last_price:.2f} -> ${price_usd:.2f}"
        if discount_percent is not None: # Show discount % if it's specifically a discount
            header_line += f"(-{discount_percent:.1f}% {price_change_prefix})"
    # For new or removed offers, this part is skipped.

    offer_body_text = format_offer_body(offer_details)
    return [header_line, offer_body_text, ""]


def append_offer_section(message_parts_list, current_item_counter, offer_list, section_title, section_separator, price_change_prefix=""): # ADDED price_change_prefix
    """Appends a formatted section of offers to the message parts list. Returns the updated item counter."""
    if offer_list:
        message_parts_list.append(f"\n{section_title}")
        message_parts_list.append(section_separator)
        item_counter = current_item_counter
        for offer in offer_list:
            item_counter += 1
            offer_block_lines = format_offer_block_lines(offer, item_counter, price_change_prefix) # Pass prefix
            message_parts_list.extend(offer_block_lines)
        if message_parts_list and message_parts_list[-1] == "":
            message_parts_list.pop()
        message_parts_list.append("=" * 15)
        return item_counter
    return current_item_counter

# --- Core Scraping Function ---
def scrape_all_offers_details(url):
    """Scrapes ALL offers, returning dict {id: details_dict} or None on failure."""
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
                'id': offer_id, 
                'description': description, 
                'seller': seller,
                'price_usd': price_usd, 
                'price_text': price_text,
                'sp_million': extracted_sp, 
                'href': href
                # 'last_seen_active' will be added during comparison logic
            }
        logging.info(f"Successfully extracted details for {len(all_offers_details)} offers.")
        return all_offers_details
    except requests.exceptions.RequestException as e:
        logging.error(f"Network/Request Error during scraping: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during scraping: {e}", exc_info=True) 
    return None

# --- Notification Functions --- (send_telegram_notification, send_discord_notification - UNCHANGED from your version)
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
    """Sends the provided message text to Discord via webhook, splitting and truncating if necessary."""
    if not message_text:
        logging.info("No message content provided to send Discord notification.")
        return False
    if not webhook_url:
        logging.error("Discord Webhook URL is missing.")
        return False

    headers = {'Content-Type': 'application/json'}
    remaining_message = message_text.strip() # Start with stripped message
    overall_success = True # Tracks if all chunks were sent successfully
    message_index = 0
    sent_any_chunk = False # Track if at least one chunk was successfully sent

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
            if split_point == -1: # No newline found, hard split at max length
                split_point = DISCORD_MAX_MSG_LENGTH
            chunk_to_send = remaining_message[:split_point]
            remaining_message = remaining_message[split_point:].lstrip() # lstrip to remove leading newlines from next part

        # --- ADDED: Proactive Truncation of the CHUNK itself ---
        part_indicator = ""
        if message_index > 1:
            part_indicator = f"(Part {message_index}) ...\n"
        if len(remaining_message) > 0: # If there's more to come
            # Reserve space for " (Continued...)" and part indicator
            reserved_space = len("...\n... (Continued in next part)") + len(part_indicator)
            if len(chunk_to_send) + reserved_space > DISCORD_MAX_MSG_LENGTH:
                chunk_to_send = chunk_to_send[:DISCORD_MAX_MSG_LENGTH - reserved_space - 3] + "..." # Truncate chunk
        elif len(part_indicator) + len(chunk_to_send) > DISCORD_MAX_MSG_LENGTH: # For the last chunk with a part indicator
             chunk_to_send = chunk_to_send[:DISCORD_MAX_MSG_LENGTH - len(part_indicator) - 3] + "..."


        # Final assembly of the chunk to send
        final_chunk = part_indicator + chunk_to_send
        if len(remaining_message) > 0:
            final_chunk += "\n... (Continued in next part)"
        
        # --- Ensure the final assembled chunk doesn't exceed the limit (failsafe) ---
        if len(final_chunk) > DISCORD_MAX_MSG_LENGTH:
            logging.warning(f"Discord chunk {message_index} after assembly is still too long ({len(final_chunk)} chars). Hard truncating.")
            # This is a last resort, should ideally be caught by logic above
            final_chunk = final_chunk[:DISCORD_MAX_MSG_LENGTH - 20] + "\n...(hard truncated)" 
        
        if not final_chunk.strip(): # Don't send empty chunks
            logging.debug(f"Skipping empty Discord chunk {message_index}.")
            continue

        payload = json.dumps({'content': final_chunk})

        try:
            logging.info(f"Sending chunk {message_index} to Discord webhook (approx {len(final_chunk)} chars)...")
            response = requests.post(webhook_url, headers=headers, data=payload, timeout=15)
            response.raise_for_status()
            logging.info(f"Discord chunk {message_index} sent successfully (Status: {response.status_code}).")
            sent_any_chunk = True # Mark that at least one chunk went through
            if len(remaining_message) > 0:
                logging.debug(f"Waiting {DISCORD_SEND_DELAY_SECONDS}s before next Discord chunk...")
                time.sleep(DISCORD_SEND_DELAY_SECONDS)
        except Exception as e:
            logging.error(f"Error sending Discord notification chunk {message_index}: {e}")
            if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
                logging.error(f"Discord Response status: {e.response.status_code}, Text: {e.response.text[:200]}...")
            overall_success = False # Mark failure
            break # Stop trying to send further chunks for this message

    if overall_success and message_index > 1 and sent_any_chunk:
        logging.info("All Discord message chunks sent successfully.")
    elif overall_success and sent_any_chunk: # Single chunk success
         logging.info("Discord notification sent successfully (single chunk).")
    elif not sent_any_chunk and message_index > 0 : # No chunks sent, means first one failed or all were empty
         logging.error("Failed to send any message chunk to Discord.")
         overall_success = False # Ensure overall_success reflects this
    # If message_index is 0, it means the original message_text was empty, already handled.

    return overall_success # Return True only if ALL intended chunks were sent

# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time_utc = datetime.now(timezone.utc)
    utc8_offset = timedelta(hours=8)
    display_timestamp = (start_time_utc + utc8_offset).strftime('%Y-%m-%d %H:%M:%S UTC+8')
    current_iso_timestamp = start_time_utc.isoformat() 

    logging.info("="*30)
    logging.info(f"Starting Funpay scraper script ({display_timestamp}) - Enriched State & NotifyOnceRemoved")

    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

    notify_via_telegram = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    notify_via_discord = bool(DISCORD_WEBHOOK_URL)

    if not notify_via_telegram and not notify_via_discord:
         logging.error("FATAL: No notification credentials (Telegram or Discord) found.")
         sys.exit("Exiting: Missing notification credentials.")
    # (logging for missing individual credentials remains)


    # 1. Load previous offer state
    previous_offer_state = load_offer_state(OFFER_STATE_FILE)

    # 2. Scrape current offers
    current_offers_scrape_dict = scrape_all_offers_details(URL)

    if current_offers_scrape_dict is None:
        logging.error("Scraping failed. Exiting without modifying state.")
        sys.exit("Exiting: Scraping function failed.")

    # 3. Compare current offers with previous state; Build notification lists & next_offer_state
    new_offers_notify = []
    discounted_offers_notify = []
    removed_offers_notify = [] 

    next_offer_state = previous_offer_state.copy()
    scraped_offer_ids = set() 

    logging.info("Processing currently scraped offers and preparing next state...")
    for offer_id, current_details_from_scrape in current_offers_scrape_dict.items():
        scraped_offer_ids.add(offer_id)
        # Always update last_seen_active for offers present in the current scrape
        current_details_from_scrape['last_seen_active'] = current_iso_timestamp 
        
        # Prepare details for next_offer_state, starting with current scrape info
        details_for_next_state = current_details_from_scrape.copy()
        # Ensure 'notified_as_removed_at' is carried over or initialized if not present
        details_for_next_state.setdefault('notified_as_removed_at', None)


        last_known_details = previous_offer_state.get(offer_id)

        if last_known_details is None:
            # Case 1: Truly New Offer
            logging.info(f"-> New Offer ID: {offer_id}. Adding to state and notification list.")
            new_offers_notify.append(current_details_from_scrape)
            # details_for_next_state is already set with current_details_from_scrape
        else:
            # Case 2: Offer was known
            # If it was previously marked as removed, it has reappeared. Reset the flag.
            if last_known_details.get('notified_as_removed_at') is not None:
                logging.info(f"-> Offer ID {offer_id} has REAPPEARED. Resetting removal notification status.")
                details_for_next_state['notified_as_removed_at'] = None # Reset flag
                # Consider if reappeared offers should always be in "new" or a "relisted" list
                # For now, let it fall through to normal price check logic.
                # If you want to notify it as "New" because it reappeared:
                new_offers_notify.append(current_details_from_scrape) # OPTIONAL: uncomment to notify relisted as new

            current_price = current_details_from_scrape.get('price_usd')
            last_price = last_known_details.get('price_usd')

            if current_price is not None and last_price is not None and last_price > 0:
                 price_diff_abs = current_price - last_price 
                 if price_diff_abs < 0: 
                      percent_decrease = abs(price_diff_abs / last_price) * 100.0
                      if percent_decrease > PRICE_CHANGE_PERCENT_THRESHOLD:
                           logging.info(f"-> Discount Found (Notify): {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Discount: {percent_decrease:.1f}%)")
                           details_for_notify = current_details_from_scrape.copy()
                           details_for_notify['last_price'] = last_price
                           details_for_notify['discount_percent'] = percent_decrease
                           discounted_offers_notify.append(details_for_notify)
                           # Price in state will be current_price (already in details_for_next_state)
                      else:
                           logging.info(f"-> Price decrease for ID {offer_id} ({percent_decrease:.1f}%) below threshold. Keeping old price in state.")
                           details_for_next_state['price_usd'] = last_price 
            elif current_price is not None and last_price is None:
                  logging.info(f"-> Price now available for ID {offer_id} (prev N/A): ${current_price:.2f}. Notifying as new.")
                  new_offers_notify.append(current_details_from_scrape) 
            elif current_price is None and last_price is not None:
                logging.warning(f"-> Price for ID {offer_id} became N/A (was ${last_price:.2f}). Updating state.")
        
        next_offer_state[offer_id] = details_for_next_state

    # Identify and process removed offers
    logging.info("Checking for removed offers...")
    previous_offer_ids = set(previous_offer_state.keys())
    removed_offer_ids_this_cycle = previous_offer_ids - scraped_offer_ids
    
    for offer_id in removed_offer_ids_this_cycle:
        if offer_id in next_offer_state: 
            # This offer was in previous state (and thus in next_offer_state from copy)
            # but not in the current scrape.
            
            # Check if we've already notified about its removal
            if next_offer_state[offer_id].get('notified_as_removed_at') is None:
                logging.info(f"-> Offer ID {offer_id} detected as REMOVED (first time). Adding to notify list.")
                removed_offers_notify.append(next_offer_state[offer_id]) # Add its last known details
                next_offer_state[offer_id]['notified_as_removed_at'] = current_iso_timestamp # Mark as notified
            else:
                logging.info(f"-> Offer ID {offer_id} was ALREADY NOTED as removed on {next_offer_state[offer_id]['notified_as_removed_at']}. Skipping notification.")
            
            # Ensure 'last_seen_active' for this "removed" offer reflects when it was *actually* last seen active.
            # It should retain its value from previous_offer_state.
            # The initial copy of next_offer_state = previous_offer_state.copy() already achieves this.
            # No explicit update to last_seen_active is needed here unless you want to mark it differently.
        else:
            logging.warning(f"Offer ID {offer_id} in removed_offer_ids_this_cycle but not found in next_offer_state. This is unexpected.")


    # Sort the notification lists
    price_sort_key = lambda item: item.get('price_usd', float('inf'))
    new_offers_notify.sort(key=price_sort_key)
    discounted_offers_notify.sort(key=price_sort_key)
    removed_offers_notify.sort(key=price_sort_key) 
    
    logging.info(f"Notification Summary: New={len(new_offers_notify)}, Discounts={len(discounted_offers_notify)}, Removed this cycle={len(removed_offers_notify)}.")

    # 5. Determine if Notification is Needed and Format Message
    notification_needed = bool(new_offers_notify or discounted_offers_notify or removed_offers_notify)
    # Initialize flags assuming failure or not attempted
    notification_sent_flags = {'telegram': False, 'discord': False} 
    full_message = ""
    any_notification_platform_succeeded = False # Track if at least one platform worked

    if notification_needed:
        logging.info("Relevant changes detected, preparing unified notification message.")
        message_parts = [f"FunPay(EVE ECHOES) Update:\n{display_timestamp}"]
        item_counter = 0

        item_counter = append_offer_section(message_parts, item_counter, new_offers_notify, "✨ New/Reappeared:", "-" * 15)
        item_counter = append_offer_section(message_parts, item_counter, discounted_offers_notify, "💰 On Sale:", "-" * 15, price_change_prefix="⬇️")
        item_counter = append_offer_section(message_parts, item_counter, removed_offers_notify, "❌ Removed/Sold(Current Cycle):", "-" * 15)

        if len(message_parts) > 1: 
            full_message = "\n".join(message_parts).strip()
        else:
            logging.warning("Notification flagged as needed, but no content sections were added. Message will be empty.")
            full_message = "" 
            # notification_needed = False # No, keep notification_needed true for state saving logic if it was set
                                        # We just won't send an empty message.

        if full_message: 
            discord_attempted_and_failed = False
            if notify_via_discord:
                logging.info("--- Attempting Discord Notification (Primary) ---")
                discord_success = send_discord_notification(DISCORD_WEBHOOK_URL, full_message)
                notification_sent_flags['discord'] = discord_success
                if discord_success:
                    any_notification_platform_succeeded = True
                else:
                    logging.error("Discord notification failed. Will attempt Telegram fallback if configured.")
                    discord_attempted_and_failed = True # Flag that Discord failed

            # Fallback to Telegram if Discord is not configured OR if Discord was configured, attempted, AND failed.
            if notify_via_telegram and (not notify_via_discord or discord_attempted_and_failed):
                if discord_attempted_and_failed:
                    logging.info("--- Attempting Telegram Notification (Fallback due to Discord failure) ---")
                elif not notify_via_discord: # This case is when Discord was never configured
                    logging.info("--- Attempting Telegram Notification (Discord not configured) ---")
                
                telegram_success = send_telegram_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, full_message)
                notification_sent_flags['telegram'] = telegram_success
                if telegram_success:
                    any_notification_platform_succeeded = True
            
            if not any_notification_platform_succeeded:
                logging.error("All configured notification attempts failed.")
        else: # full_message was empty
             logging.info("Notification message was empty, no notifications sent.")

    else: # notification_needed was False
        logging.info("No new offers, significant discounts, or reappeared offers detected for notification.")

    # --- SAVE STATE FILE ---
    # Save state regardless of notification success, as long as scraping was successful
    # and processing reached this point.
    logging.info(f"Saving state for {len(next_offer_state)} offers (includes current, preserved, and removal-notified offers).")
    save_offer_state(OFFER_STATE_FILE, next_offer_state)
    # --- END SAVE STATE FILE ---

    end_time_utc = datetime.now(timezone.utc)
    duration = (end_time_utc - start_time_utc).total_seconds()
    logging.info(f"Script finished in {duration:.2f} seconds.")
    
    # Log final notification status
    if notification_needed and full_message: 
        # Construct more detailed status message
        status_parts = []
        if notify_via_discord:
            status_parts.append(f"Discord: {'OK' if notification_sent_flags['discord'] else 'FAIL'}")
        else:
            status_parts.append("Discord: Not Configured")
        
        if notify_via_telegram:
            # Check if Telegram was attempted (either primary or fallback)
            if (not notify_via_discord or discord_attempted_and_failed):
                 status_parts.append(f"Telegram: {'OK' if notification_sent_flags['telegram'] else 'FAIL'}")
            else: # Telegram was not attempted because Discord was primary and succeeded
                 status_parts.append("Telegram: Skipped (Discord OK)")
        else:
            status_parts.append("Telegram: Not Configured")
        logging.info(f"Notification Status: {', '.join(status_parts)}")

    logging.info("="*30)

