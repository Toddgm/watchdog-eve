import requests
from bs4 import BeautifulSoup
import time
import logging
import re
import os
import json # For handling state file
from urllib.parse import urlparse, parse_qs
import sys
if os.path.exists('.env'):
    from dotenv import load_dotenv
    load_dotenv()

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OFFER_STATE_FILE = "offer_state.json" # Stores {offer_id: last_price}
PRICE_CHANGE_THRESHOLD = 5.00 # Ignore price changes <= this value (in USD)
# INCLUDE_PRICE_INCREASES_IN_MSG = False # Set to True if you want to see price increases too (Currently ON)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,application/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
}
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT = 20
TELEGRAM_MAX_MSG_LENGTH = 4096
DESCRIPTION_TRUNCATE_LENGTH = 90 # Max chars for description in message
# Add cookie to force USD currency display
COOKIES = {
    'cy': 'USD'
}
# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

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
                 logging.warning(f"Invalid price type '{type(price)}' for ID {offer_id} in state file. Skipping.")
        logging.info(f"Loaded state for {len(validated_state)} offers from '{filename}'.")
        return validated_state
    except Exception as e:
        logging.error(f"Error loading state from '{filename}': {e}. Starting with empty state.")
    return {}

def save_offer_state(filename, current_state):
    """Saves the current offer state ({offer_id: current_price}) to a JSON file."""
    # Ensure only valid price entries are saved (float or None)
    state_to_save = {}
    for offer_id, price in current_state.items():
         if isinstance(price, (int, float, type(None))): # Allow None price in state file
             state_to_save[str(offer_id)] = float(price) if isinstance(price, (int, float)) else None
         else:
             logging.warning(f"Attempted to save invalid price type '{type(price)}' for ID {offer_id}. Skipping.")

    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(state_to_save, f, indent=2, ensure_ascii=False)
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
    """Attempts to extract Skill Points (in millions) strictly from the end of the description."""
    if not description: return None
    # Remove commas and leading/trailing whitespace for consistent processing
    description_lower = description.lower().replace(',', '').strip()

    # Pattern: Looks for optional leading space/comma, then number, strictly " m sp", optional trailing space, END
    # Example matches: ", 100 m sp", "120 m sp", " 50.5 m sp"
    # This pattern is anchored to the end ($) and specifically looks for the " m sp" unit.
    strict_end_pattern = r'[\s,]*(\d+(?:\.\d+)?)\s*m\s*sp\s*$'

    match = re.search(strict_end_pattern, description_lower)

    if match:
        sp_value_str = match.group(1)
        try:
            # Based on the strict rule, the number preceding "m sp" is the SP in millions.
            return float(sp_value_str)
        except ValueError:
            logging.warning(f"Could not convert extracted strict end-SP value '{sp_value_str}' to float.")
            return None # If conversion fails, it's not a valid SP number
    else:
        logging.debug(f"Strict 'X m SP' pattern not found at end of description: '{description}'")
        return None # Pattern not found


def format_offer_body(offer_details):
    """Formats the core details of a single offer (description, price, sp, link) for the message."""
    desc = ' '.join(offer_details['description'].split())
    # Attempt to preserve the SP part at the end during truncation
    if len(desc) > DESCRIPTION_TRUNCATE_LENGTH:
        sp_match = re.search(r'\d+(?:\.\d+)?\s*m\s*sp\s*$', desc.lower())
        if sp_match:
             # Calculate length from the start of the SP match to the end of the string
             keep_length = len(desc) - sp_match.start()
             # If the SP part is shorter than the desired total truncated length,
             # include enough text from before the SP part.
             if keep_length < DESCRIPTION_TRUNCATE_LENGTH:
                  # Truncate the beginning of the description
                  truncate_point = DESCRIPTION_TRUNCATE_LENGTH - keep_length
                  # Ensure we don't create empty strings or index errors
                  start_index = max(0, len(desc) - DESCRIPTION_TRUNCATE_LENGTH + 3)
                  desc = "..." + desc[start_index:]
             else:
                 # If the SP part is already long, just show it with "..." preceding
                 desc = "..." + desc[sp_match.start():]
        else:
             # Fallback to simple truncation if SP pattern isn't found at the end
             desc = desc[:DESCRIPTION_TRUNCATE_LENGTH] + "..."
    # else: desc remains as is if short enough

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

def format_offer_block_lines(offer_details, item_number, price_change_prefix=""):
    """Formats a complete block of text for a single offer including header, ratio, and body."""
    price_usd = offer_details.get('price_usd')
    sp_million = offer_details.get('sp_million')
    ratio_str = ""

    # Calculate and format price per SP ratio
    if price_usd is not None and sp_million is not None and sp_million > 0:
        try:
            price_per_million = price_usd / sp_million
            ratio_str = f" [${price_per_million:.2f}/mil]"
        except Exception as e:
             logging.warning(f"Could not calculate price/SP ratio for offer {offer_details['id']}: {e}")
             pass # Keep ratio_str empty

    # Build the header line with counter and ratio
    header_line = f"#{item_number}{ratio_str}"

    # Add price change info if applicable
    if price_change_prefix and offer_details.get('last_price') is not None and price_usd is not None:
        last_price = offer_details['last_price']
        header_line += f" ({price_change_prefix}: ${last_price:.2f} -> ${price_usd:.2f})"


    offer_body = format_offer_body(offer_details)

    # Return a list of strings representing the complete block for this offer
    return [header_line, offer_body, ""] # Header, body, blank line below

def append_offer_section(message_parts_list, current_item_counter, offer_list, section_title, section_separator, price_change_prefix=""):
    """Appends a formatted section of offers to the message parts list. Returns the updated item counter."""
    if offer_list:
        message_parts_list.append(f"\n{section_title}")
        message_parts_list.append(section_separator)
        item_counter = current_item_counter # Start counting from the passed value
        for offer in offer_list:
            item_counter += 1
            # Use the separate function to get formatted lines for one offer block
            offer_block_lines = format_offer_block_lines(offer, item_counter, price_change_prefix)
            message_parts_list.extend(offer_block_lines)

        # Clean up the last blank line added by format_offer_block_lines if it's the very last thing
        if message_parts_list and message_parts_list[-1] == "":
            message_parts_list.pop()
        message_parts_list.append("=" * 15) # Section end
        return item_counter # Return the counter after processing this section
    return current_item_counter # Return the same counter if the list was empty


# --- Core Scraping Function ---
def scrape_all_offers_details(url):
    """Scrapes ALL offers, returning dict {id: details} or None on failure."""
    logging.info(f"Attempting to fetch and parse ALL offers from: {url}")
    all_offers_details = {}
    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds...")
        time.sleep(REQUEST_DELAY_SECONDS)
         # ADDED: Pass the COOKIES dictionary to the requests.get call
        logging.info("Adding cookie 'cy=USD' to request.")
        response = requests.get(url, headers=HEADERS, cookies=COOKIES, timeout=REQUEST_TIMEOUT)
        logging.info(f"Response status code: {response.status_code}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers.")
        if not offer_containers:
            logging.warning("No offer containers found on the page.")
            return {} # Return empty dict if no offers but request was successful

        for container in offer_containers:
            href = container.get('href')
            offer_id = extract_offer_id_from_href(href)
            if not offer_id: continue # Skip if ID can't be extracted

            desc_tag = container.find('div', class_='tc-desc-text')
            description = desc_tag.get_text(separator=' ', strip=True) if desc_tag else "N/A"
            seller_tag = container.find('div', class_='media-user-name')
            seller = seller_tag.get_text(strip=True) if seller_tag else "N/A"
            price_container_tag = container.find('div', class_='tc-price')
            price_text = price_container_tag.get_text(strip=True) if price_container_tag else "N/A"
            price_usd = None
            # Attempt to parse price. Funpay prices can be complex.
            # This pattern tries to find a number with optional thousands separators/decimals near currency symbols
            # or just a plain number. Removes spaces and commas before parsing.
            cleaned_price_text = price_text.replace(' ', '').replace(',', '').replace('$', '').replace('€', '').replace('£', '')
            try:
                # Use a simple float conversion after cleaning, assuming the first number is the price
                price_match = re.search(r'(\d+\.?\d*)', cleaned_price_text)
                if price_match:
                     price_usd = float(price_match.group(1))
                # else price_usd remains None
            except ValueError:
                 logging.warning(f"Offer ID {offer_id}: Could not convert parsed price string '{price_match.group(1) if price_match else cleaned_price_text}' to float from text '{price_text}'")
            except Exception as e:
                 logging.warning(f"Offer ID {offer_id}: Unexpected error parsing price '{price_text}': {e}")


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
    return None # Return None on critical failure

# --- Telegram Notification Function ---
def send_telegram_notification(bot_token, chat_id, message_text):
    """Sends the provided message text to Telegram."""
    if not message_text:
        logging.info("No message content provided to send notification.")
        return False
    if not bot_token or not chat_id:
        logging.error("Telegram Bot Token or Chat ID is missing.")
        return False
    # Encode and check byte length before truncating
    message_bytes = message_text.encode('utf-8')
    if len(message_bytes) > TELEGRAM_MAX_MSG_LENGTH:
        logging.warning(f"Message length exceeds limit ({TELEGRAM_MAX_MSG_LENGTH} bytes). Truncating.")
        # Truncate by bytes, then decode. Leave space for the truncation message.
        truncated_bytes = message_bytes[:TELEGRAM_MAX_MSG_LENGTH - 30].decode('utf-8', 'ignore').encode('utf-8')
        message_text = truncated_bytes.decode('utf-8', 'ignore') + "\n... (truncated)"


    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Using simple text mode to avoid Markdown escaping issues
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
        # Do NOT save state if scraping failed
        sys.exit("Exiting: Scraping function failed.")

    # 3. Compare current offers with previous state
    new_offers = []
    price_decreased = []
    price_increased = []

    # Prepare the state for the *next* run. This will contain *all* currently found offers with their *latest* prices.
    # Offers from previous_offer_state that are *not* in current_offers_details are implicitly removed from tracking state.
    next_offer_state = {}

    logging.info("Comparing current offers to previous state...")
    for offer_id, current_details in current_offers_details.items():
        current_price = current_details.get('price_usd')
        last_price = previous_offer_state.get(offer_id)

        # Always add the current price (or None if N/A) to the state for the next run
        next_offer_state[offer_id] = current_price

        if offer_id not in previous_offer_state:
            logging.info(f"-> Found New Offer: {offer_id}")
            new_offers.append(current_details)
        else:
            # Offer exists in previous state, check for price change
            if current_price is not None and last_price is not None:
                price_diff = current_price - last_price
                abs_price_diff = abs(price_diff)

                if abs_price_diff > PRICE_CHANGE_THRESHOLD:
                    # Add last price to the current details dict for easier formatting later
                    current_details['last_price'] = last_price
                    if price_diff < 0: # Price decreased
                        logging.info(f"-> Price Decrease: {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Diff: ${abs_price_diff:.2f})")
                        price_decreased.append(current_details)
                    elif price_diff > 0: # Price increased
                         logging.info(f"-> Price Increase: {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Diff: ${abs_price_diff:.2f})")
                         price_increased.append(current_details)
                else:
                    # Price change is below the threshold
                    logging.info(f"-> Price change below threshold for {offer_id} (${last_price:.2f} -> ${current_price:.2f}, Diff: ${abs_price_diff:.2f}). Ignoring notification.")
                    # Offer's new price is correctly captured in next_offer_state above, but no notification is sent this run.
            elif current_price is not None and last_price is None:
                 # Price wasn't available last time but is now available and numeric.
                 # Treat this as a new offer to highlight it.
                 logging.info(f"-> Price now available for {offer_id} (previously N/A): ${current_price:.2f}. Notifying as new.")
                 new_offers.append(current_details) # Add to new_offers list
            elif current_price is None and last_price is not None:
                 # Price was available but is now N/A. Log this warning, state will capture None.
                 logging.warning(f"-> Price is now N/A for {offer_id} (was ${last_price:.2f}). State updated to None.")


    # 4. Sort the lists by price
    # Sort each list by 'price_usd'. Use float('inf') for None prices to put them last.
    price_sort_key = lambda item: item.get('price_usd', float('inf'))
    new_offers.sort(key=price_sort_key)
    price_decreased.sort(key=price_sort_key)
    price_increased.sort(key=price_sort_key)
    logging.info(f"Sorted {len(new_offers)} new offers, {len(price_decreased)} decreased, {len(price_increased)} increased by price (ascending).")

    # 5. Format and Send Notification (if anything changed significantly)
    notification_needed = bool(new_offers or price_decreased or price_increased)
    notification_sent = False

    if notification_needed:
        logging.info("Changes detected above threshold, preparing notification message.")
        message_parts = ["FunPay(EVE ECHOES) Update:"]
        item_counter = 0 # Use a single counter for all items in the main block

        # Append sections to message parts using the independent helper function
        item_counter = append_offer_section(message_parts, item_counter, new_offers, "✨ New Offers:", "-" * 15)
        item_counter = append_offer_section(message_parts, item_counter, price_decreased, "💲⬇️ Price Decreases:", "-" * 10, price_change_prefix="⬇️")
        item_counter = append_offer_section(message_parts, item_counter, price_increased, "💲⬆️ Price Increases:", "-" * 10, price_change_prefix="⬆️")

        full_message = "\n".join(message_parts)

        notification_sent = send_telegram_notification(
            TELEGRAM_BOT_TOKEN,
            TELEGRAM_CHAT_ID,
            full_message
        )
    else:
        logging.info("No new offers or significant price changes detected.")

    # 6. Save the *current* state to the file for the next run
    # This state includes the latest prices (or None) for all currently scraped offers.
    # It correctly handles offers with price changes below threshold (updates state, no notify)
    # and offers that were removed (implicitly removed from state).
    save_offer_state(OFFER_STATE_FILE, next_offer_state)

    end_time = time.time()
    logging.info(f"Script finished in {end_time - start_time:.2f} seconds.")
    logging.info("="*30)