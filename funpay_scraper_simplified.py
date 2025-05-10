# --- START OF FILE FunPay_scraper_simplified.py ---

import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime, timedelta, timezone
import logging
import re
import os
import json # Only used for Discord webhook payload now
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
PRICE_THRESHOLD_USD = 60.0  # Notify if price is BELOW this USD value
SP_THRESHOLD_MILLION = 0.0 # Notify if SP is ABOVE this value (in millions)

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

# --- Helper Functions (Retained and slightly simplified where state was involved) ---

# Removed load_offer_state and save_offer_state entirely

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
            SANITY_CHECK_THRESHOLD = 1000 # If extracted number is > 1000, it's probably raw SP
            if sp_value > SANITY_CHECK_THRESHOLD:
                 corrected_sp_value = sp_value / 1_000_000.0
                 logging.warning(
                     f"Offer text seems to contain raw SP '{sp_value_str}' instead of M SP. "
                     f"Converting to {corrected_sp_value:.2f}M SP."
                 )
                 return corrected_sp_value
            return sp_value
        except ValueError:
            logging.warning(f"Could not convert extracted strict end-SP value '{sp_value_str}' to float.")
            return None
    else:
        # Debug logging for failed strict pattern match
        # logging.debug(f"Strict 'X m SP' pattern not found at end of description: '{description}'")
        return None # Pattern not found

def format_offer_body(offer_details):
    """Formats the core details of a single offer (description, price, sp, link) for the message."""
    desc = ' '.join(offer_details.get('description', "N/A").split())
    if len(desc) > DESCRIPTION_TRUNCATE_LENGTH:
        # Attempt to preserve the SP part at the end during truncation
        sp_match = re.search(r'\d+(?:\.\d+)?\s*m\s*sp\s*$', desc.lower())
        if sp_match:
             keep_length = len(desc) - sp_match.start()
             if keep_length < DESCRIPTION_TRUNCATE_LENGTH:
                  start_index = max(0, len(desc) - DESCRIPTION_TRUNCATE_LENGTH + 3)
                  desc = "..." + desc[start_index:]
             else:
                 desc = "..." + desc[sp_match.start():]
        else:
             # Fallback to simple truncation if SP pattern isn't found at the end
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

def format_offer_block_lines(offer_details, item_number): # Removed price_change_prefix
    """Formats a complete block of text for a single offer including header, ratio, and body."""
    price_usd = offer_details.get('price_usd')
    sp_million = offer_details.get('sp_million')
    ratio_str = ""

    if price_usd is not None and sp_million is not None and sp_million > 0:
        try:
            price_per_million = price_usd / sp_million
            ratio_str = f" [${price_per_million:.2f}/mil]"
        except Exception as e:
             logging.warning(f"Could not calculate price/SP ratio for offer {offer_details.get('id', 'N/A')}: {e}")

    # Header line with item number and ratio
    header_line = f"#{item_number}{ratio_str}"

    offer_body_text = format_offer_body(offer_details)
    return [header_line, offer_body_text, ""] # Header, body, blank line below

def append_offer_section(message_parts_list, current_item_counter, offer_list, section_title, section_separator): # Removed price_change_prefix
    """Appends a formatted section of offers to the message parts list. Returns the updated item counter."""
    if offer_list:
        message_parts_list.append(f"\n{section_title}")
        message_parts_list.append(section_separator)
        item_counter = current_item_counter
        for offer in offer_list:
            item_counter += 1
            offer_block_lines = format_offer_block_lines(offer, item_counter) # Don't pass prefix
            message_parts_list.extend(offer_block_lines)
        if message_parts_list and message_parts_list[-1] == "":
            message_parts_list.pop()
        message_parts_list.append("=" * 15)
        return item_counter
    return current_item_counter

# --- Core Scraping Function (Remains largely unchanged) ---
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
            # Attempt to parse price. Funpay prices can be complex.
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
            }
        logging.info(f"Successfully extracted details for {len(all_offers_details)} offers.")
        return all_offers_details
    except requests.exceptions.RequestException as e:
        logging.error(f"Network/Request Error during scraping: {e}")
    except Exception as e:
        logging.error(f"Unexpected error during scraping: {e}", exc_info=True)
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
        
        # If there's more to come, add a continuation indicator to the current chunk
        continuation_indicator = ""
        if len(remaining_message) > 0:
             continuation_indicator = "\n... (Continued in next part)"

        # Check if adding indicators makes the chunk exceed the limit and truncate if needed
        reserved_space = len(part_indicator) + len(continuation_indicator)
        if len(chunk_to_send) + reserved_space > DISCORD_MAX_MSG_LENGTH:
            chunk_to_send = chunk_to_send[:DISCORD_MAX_MSG_LENGTH - reserved_space - 3] + "..." # Truncate chunk body

        # Final assembly of the chunk to send
        final_chunk = part_indicator + chunk_to_send + continuation_indicator
        
        # --- Ensure the final assembled chunk doesn't exceed the limit (failsafe) ---
        if len(final_chunk) > DISCORD_MAX_MSG_LENGTH:
            logging.warning(f"Discord chunk {message_index} after assembly is still too long ({len(final_chunk)} chars). Hard truncating.")
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
            sent_any_chunk = True
            if len(remaining_message) > 0:
                logging.debug(f"Waiting {DISCORD_SEND_DELAY_SECONDS}s before next Discord chunk...")
                time.sleep(DISCORD_SEND_DELAY_SECONDS)
        except Exception as e:
            logging.error(f"Error sending Discord notification chunk {message_index}: {e}")
            if isinstance(e, requests.exceptions.RequestException) and e.response is not None:
                logging.error(f"Discord Response status: {e.response.status_code}, Text: {e.response.text[:200]}...")
            overall_success = False
            break

    if overall_success and message_index > 0 and sent_any_chunk: # Check message_index > 0 to know if original message wasn't empty
        logging.info("All Discord message chunks processed.")
    elif overall_success and message_index == 0:
         logging.info("No chunks to send for Discord (message was empty).")
    elif not overall_success:
         logging.error("Discord notification failed or partially failed.")


    return overall_success # Return True only if ALL intended chunks were sent without break


# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time_utc = datetime.now(timezone.utc)
    utc8_offset = timedelta(hours=8)
    display_timestamp = (start_time_utc + utc8_offset).strftime('%Y-%m-%d %H:%M:%S UTC+8')

    logging.info("="*30)
    logging.info(f"Starting Simplified Funpay Scraper ({display_timestamp})")

    TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
    TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
    DISCORD_WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK_URL')

    notify_via_telegram = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)
    # notify_via_discord = bool(DISCORD_WEBHOOK_URL)
    notify_via_discord = false # disable discord notify

    if not notify_via_telegram and not notify_via_discord:
         logging.error("FATAL: No notification credentials (Telegram or Discord) found.")
         sys.exit("Exiting: Missing notification credentials.")
    else:
         if notify_via_telegram: logging.info("Telegram notifications ENABLED.")
         if notify_via_discord: logging.info("Discord notifications ENABLED.")


    # 1. Scrape current offers
    current_offers_scrape_dict = scrape_all_offers_details(URL)
    offer_count = len(current_offers_scrape_dict)
    logging.info(f"Total # offer found:{offer_count}")
    
    if current_offers_scrape_dict is None:
        logging.error("Scraping failed. Exiting.")
        sys.exit("Exiting: Scraping function failed.")

    # 2. Filter offers based on simplified criteria
    matching_offers = []
    logging.info(f"Filtering scraped offers by criteria: Price < ${PRICE_THRESHOLD_USD:.2f} AND SP > {SP_THRESHOLD_MILLION:.1f}M")

    for offer_id, details in current_offers_scrape_dict.items():
        price_usd = details.get('price_usd')
        sp_million = details.get('sp_million')

        if price_usd is not None and sp_million is not None:
            if price_usd < PRICE_THRESHOLD_USD and sp_million > SP_THRESHOLD_MILLION:
                logging.info(f"-> Offer ID {offer_id} matches criteria (Price: ${price_usd:.2f}, SP: {sp_million:.1f}M).")
                matching_offers.append(details)
            # else: logging.debug(f"Offer ID {offer_id} does not match criteria (Price: {price_usd}, SP: {sp_million})")
        # else: logging.debug(f"Offer ID {offer_id} missing price or SP (Price: {price_usd}, SP: {sp_million})")

    # 3. Sort matching offers by price (ascending)
    price_sort_key = lambda item: item.get('price_usd', float('inf'))
    matching_offers.sort(key=price_sort_key)

    logging.info(f"Found {len(matching_offers)} offer(s) matching the criteria.")

    # 4. Format and Send Notification
    notification_needed = bool(matching_offers) # Only notify if we found matches
    full_message = ""
    any_notification_platform_succeeded = False # Track if at least one platform worked

    if notification_needed:
        logging.info("Matching offers found, preparing notification message.")
        message_parts = [f"FunPay(EVE ECHOES) Update:",
                         f"[Total {offer_count} found]",
                         f"{display_timestamp}"]

        item_counter = 0 # Use a single counter

        # Append the section of matching offers using the helper
        # We reuse append_offer_section, but it's simpler now (only one list)
        item_counter = append_offer_section(message_parts, item_counter, matching_offers, ">>Matching Offers", "-" * 15)


        if len(message_parts) > 3: # Check if any offers were actually added below the header
            full_message = "\n".join(message_parts).strip()
        else:
            logging.warning("Notification flagged as needed, but no offers were added to the message parts.")
            full_message = ""


        if full_message:
            discord_attempted_and_failed = False
            if notify_via_discord:
                logging.info("--- Attempting Discord Notification (Primary) ---")
                discord_success = send_discord_notification(DISCORD_WEBHOOK_URL, full_message)
                if discord_success:
                    any_notification_platform_succeeded = True
                else:
                    logging.error("Discord notification failed.")
                    discord_attempted_and_failed = True # Flag that Discord failed

            # Fallback to Telegram if Discord is not configured OR if Discord was configured, attempted, AND failed.
            if notify_via_telegram and (not notify_via_discord or discord_attempted_and_failed):
                if discord_attempted_and_failed:
                    logging.info("--- Attempting Telegram Notification (Fallback due to Discord failure) ---")
                elif not notify_via_discord: # This case is when Discord was never configured
                    logging.info("--- Attempting Telegram Notification (Discord not configured) ---")

                telegram_success = send_telegram_notification(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, full_message)
                if telegram_success:
                    any_notification_platform_succeeded = True

            if not any_notification_platform_succeeded:
                logging.error("All configured notification attempts failed for the matching offers message.")
            else:
                logging.info("Notification for matching offers sent successfully via at least one platform.")
        else: # full_message was empty despite matching_offers list not being empty (shouldn't happen with logic, but defensive)
             logging.info("Notification message content was empty, no notifications sent.")

    else: # notification_needed was False because matching_offers was empty
        logging.info("No offers found matching the criteria. No notifications sent.")


    # 5. No state file saving in this simplified version

    end_time_utc = datetime.now(timezone.utc)
    duration = (end_time_utc - start_time_utc).total_seconds()
    logging.info(f"Script finished in {duration:.2f} seconds.")
    logging.info("="*30)
