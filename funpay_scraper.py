import requests
from bs4 import BeautifulSoup
import time
import logging
import re
import os # Needed for file existence check
from urllib.parse import urlparse, parse_qs # For extracting URL parameters

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OFFERS_OUTPUT_FILE = "offers.txt" # File for current new offers for notification
PROCESSED_IDS_FILE = "processed_ids.txt" # File to store IDs already processed/notified
MAX_PRICE_USD = 50.00
MIN_SP_MILLION = 20.0 # Minimum Skill Points required (in Millions)

HEADERS = {
    # Mimic a browser to avoid simple blocks
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1',
}
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT = 20

# --- Logging Setup ---
# Use INFO level for general operation, DEBUG for verbose details if needed
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Functions ---

def load_processed_ids(filename):
    """Loads previously processed offer IDs from a file into a set."""
    processed_ids = set()
    # Check if the file exists before trying to open it
    if not os.path.exists(filename):
        logging.info(f"Processed IDs file '{filename}' not found. Assuming first run or no previous IDs.")
        return processed_ids # Return the empty set

    try:
        # Open the file for reading with UTF-8 encoding
        with open(filename, 'r', encoding='utf-8') as f:
            # Read each line from the file
            for line in f:
                # Remove leading/trailing whitespace (like newlines)
                line = line.strip()
                # Ensure the line is not empty and contains only digits
                if line and line.isdigit():
                    processed_ids.add(line)
                elif line: # Log if a non-empty line wasn't purely digits
                    logging.warning(f"Skipping non-digit or empty line in '{filename}': '{line}'")

        logging.info(f"Loaded {len(processed_ids)} processed IDs from '{filename}'.")

    except IOError as e:
        # Handle potential errors during file reading
        logging.error(f"Error reading processed IDs file '{filename}': {e}")
        return set() # Return empty set on read error to be safe
    except Exception as e:
        # Catch any other unexpected errors during loading
        logging.error(f"Unexpected error loading processed IDs from '{filename}': {e}")
        return set()

    return processed_ids

def append_processed_ids(filename, new_ids):
    """Appends newly processed offer IDs to the file, one ID per line."""
    if not new_ids:
        return # Nothing to append
    try:
        # Open in append mode ('a'), create if doesn't exist
        with open(filename, 'a', encoding='utf-8') as f:
            for offer_id in new_ids:
                f.write(f"{offer_id}\n") # Write each ID on a new line
        logging.info(f"Appended {len(new_ids)} new IDs to '{filename}'.")
    except IOError as e:
        logging.error(f"Error appending to processed IDs file '{filename}': {e}")
    except Exception as e:
        logging.error(f"Unexpected error appending processed IDs to '{filename}': {e}")


def extract_offer_id_from_href(href):
    """Extracts the offer ID from the 'id' query parameter of a URL."""
    if not href:
        return None
    try:
        parsed_url = urlparse(href)
        # Check if the path is as expected (optional but good practice)
        if parsed_url.path != '/en/lots/offer':
             logging.debug(f"Href path '{parsed_url.path}' is not the expected offer path.")
             # Depending on strictness, you might return None here
             # return None
        query_params = parse_qs(parsed_url.query)
        # parse_qs returns a list for each param, get the first element or None
        offer_id = query_params.get('id', [None])[0]
        if offer_id and offer_id.isdigit():
            return offer_id
        else:
            logging.warning(f"Found 'id' parameter but it's not purely digits: '{offer_id}' in href '{href}'")
    except Exception as e:
        logging.warning(f"Could not parse offer ID from href '{href}': {e}")
    return None

def extract_sp_from_description(description):
    """
    Attempts to extract Skill Points (in millions) from the description text.
    Returns float SP value if found, otherwise None.
    Assumes numbers near 'sp' refer to millions unless 'k' is present.
    """
    # Case-insensitive search
    description_lower = description.lower()

    # Define patterns to search for SP information
    patterns = [
        r'(\d+(?:\.\d+)?)\s*(?:m|mil|million)\s*sp', # e.g., "150m sp", "150 million sp"
        r'sp\s*(\d+(?:\.\d+)?)\s*(?:m|mil|million)', # e.g., "sp 150m", "sp: 150 million"
        r'(\d+(?:\.\d+)?)\s*sp'                     # e.g., "150 sp" (ASSUMES millions) - place last
    ]

    for pattern in patterns:
        match = re.search(pattern, description_lower)
        if match:
            # Check if 'k' (for thousand) is nearby, indicating it's NOT millions
            potential_k_context = description_lower[max(0, match.start()-5):min(len(description_lower), match.end()+5)]
            # Check if 'k sp' or 'k ' is in the context, or if 'k' immediately precedes the matched number
            is_thousand = False
            if 'k sp' in potential_k_context or 'k ' in potential_k_context:
                 is_thousand = True
            # Check character immediately before the number's start index
            if match.start(1) > 0 and description_lower[match.start(1)-1:match.start(1)] == 'k':
                 is_thousand = True

            if is_thousand:
                 logging.debug(f"Found 'k' near SP match in '{description[:50]}...', likely not millions. Skipping this pattern match.")
                 continue # Skip this match if 'k' seems present

            try:
                sp_value = float(match.group(1))
                logging.debug(f"Extracted SP value: {sp_value} from description using pattern: '{pattern}'")
                return sp_value # Return the first valid match found
            except (ValueError, IndexError):
                logging.warning(f"Could not convert extracted SP '{match.group(1)}' to float.")
                continue # Try next pattern if conversion fails

    # If no pattern matched or all matches were potentially 'k'
    logging.debug(f"Could not find valid SP value (in millions) in description: '{description[:50]}...'")
    return None


def scrape_funpay_offers(url, max_price, min_sp, processed_ids_set):
    """
    Scrapes offers from the Funpay URL, filters by price/SP, checks against
    processed IDs, and returns a list of NEW matching offers and their IDs.
    """
    logging.info(f"Attempting to fetch URL: {url}")
    new_matching_offers = []
    newly_found_ids = set() # Keep track of IDs found in *this* run that meet criteria AND are new

    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds before request...")
        time.sleep(REQUEST_DELAY_SECONDS)

        logging.info(f"Sending GET request") # Removed headers from log for brevity
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        logging.info(f"Received response with status code: {response.status_code}")
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        logging.info("Successfully fetched page content.")

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all offer containers (<a> tags with class 'tc-item')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers using selector: ('a', class_='tc-item')")

        if not offer_containers:
             body_text = soup.body.get_text(strip=True) if soup.body else ""
             if "checking your browser" in body_text.lower() or "enable javascript" in body_text.lower():
                 logging.error("Page seems to require JavaScript or is performing a browser check.")
             else:
                 logging.warning("No offer containers found matching the specified selector.")
             return [], set() # Return empty lists/sets

        processed_count = 0
        eligible_count = 0
        new_count = 0
        for container in offer_containers:
            processed_count += 1

            # --- Extract Offer ID ---
            href = container.get('href')
            offer_id = extract_offer_id_from_href(href)
            if not offer_id:
                logging.warning(f"Could not extract valid Offer ID from href: {href}. Skipping container.")
                continue

            # --- Extract other details ---
            desc_tag = container.find('div', class_='tc-desc-text')
            description = desc_tag.get_text(separator=' ', strip=True) if desc_tag else "N/A"

            seller_tag = container.find('div', class_='media-user-name')
            seller = seller_tag.get_text(strip=True) if seller_tag else "N/A"

            price_container_tag = container.find('div', class_='tc-price')
            price_text = price_container_tag.get_text(strip=True) if price_container_tag else None

            if not price_text:
                logging.warning(f"Offer ID {offer_id}: Could not find price text. Skipping.")
                continue

            # --- Process Price ---
            price_usd = None
            try:
                # Regex to find price number, allowing for currency symbols and commas
                price_match = re.search(r'[\$€£]?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)', price_text.replace(',', ''))
                if price_match:
                    price_usd = float(price_match.group(1))
                else:
                    # Fallback if the primary regex fails (e.g., just a number)
                    fallback_match = re.search(r'(\d+\.?\d*)', price_text)
                    if fallback_match:
                        price_usd = float(fallback_match.group(1))
                        logging.warning(f"Offer ID {offer_id}: Used fallback regex for price: '{price_text}' -> {price_usd}")
                    else:
                         raise ValueError(f"No numeric price found in text: '{price_text}'")
            except ValueError as ve:
                logging.error(f"Offer ID {offer_id}: Could not convert price text '{price_text}' to float. Error: {ve}. Skipping.")
                continue
            except Exception as e:
                 logging.error(f"Offer ID {offer_id}: Unexpected error processing price: {e}. Skipping.")
                 continue

            # --- Extract SP from Description ---
            extracted_sp = extract_sp_from_description(description)

            # --- Filtering Logic ---
            price_ok = price_usd is not None and price_usd < MAX_PRICE_USD
            sp_ok = extracted_sp is not None and extracted_sp >= MIN_SP_MILLION

            # Check if meets BOTH Price/SP criteria
            if price_ok and sp_ok:
                eligible_count += 1
                # --- Check against processed IDs ---
                if offer_id not in processed_ids_set:
                    # This is a NEW offer meeting all criteria
                    new_count += 1
                    logging.info(f"--> NEW MATCH FOUND: Offer ID {offer_id}, Price ${price_usd:.2f}, SP {extracted_sp:.1f}M")
                    offer_data = {
                        'id': offer_id, # Store the ID
                        'description': description,
                        'seller': seller,
                        'price_usd': price_usd,
                        'price_text': price_text,
                        'sp_million': extracted_sp,
                        'href': href # Store original link
                    }
                    new_matching_offers.append(offer_data)
                    newly_found_ids.add(offer_id) # Add to the set of IDs found *this run* that are new
                else:
                    # Meets criteria, but was already processed in a previous run
                    logging.debug(f"Offer ID {offer_id}: Meets criteria but already processed. Skipping notification.")
            # Optional: Log rejections for debugging
            # else:
            #     reason = []
            #     if not price_ok: reason.append(f"Price (${price_usd:.2f})")
            #     if not sp_ok: reason.append(f"SP ({extracted_sp}M)")
            #     logging.debug(f"Offer ID {offer_id}: Rejected on {', '.join(reason)}. Desc: {description[:50]}...")


    except requests.exceptions.Timeout:
        logging.error(f"Request timed out after {REQUEST_TIMEOUT} seconds.")
        return [], set()
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP Error: {e.response.status_code} {e.response.reason} for URL: {url}")
        if 400 <= e.response.status_code < 500:
             logging.error("Client error - check headers, URL, or potentially IP blocking.")
        return [], set()
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")
        return [], set()
    except AttributeError as e:
         logging.error(f"AttributeError during parsing: {e}. Check selectors against website structure.")
         return [], set()
    except Exception as e:
        logging.error(f"An unexpected error occurred during scraping: {e}")
        return [], set()

    logging.info(f"Completed scraping. Processed {processed_count} offers.")
    logging.info(f"Found {eligible_count} offers meeting Price < ${MAX_PRICE_USD:.2f} AND SP >= {MIN_SP_MILLION:.1f}M criteria.")
    logging.info(f"Identified {len(new_matching_offers)} as NEW offers (not previously processed).")
    return new_matching_offers, newly_found_ids


def save_new_offers_to_file(offers, filename, max_price, min_sp, url):
    """
    Saves the list of NEW filtered offers to the specified file.
    Only creates the file if the offers list is not empty.
    Returns True if the file was created/updated, False otherwise.
    """
    if not offers:
        # No new offers, ensure the output file does not exist from a previous run
        if os.path.exists(filename):
             try:
                 os.remove(filename)
                 logging.info(f"Removed existing '{filename}' as no new offers were found.")
             except OSError as e:
                 logging.error(f"Error removing existing '{filename}': {e}")
        else:
             logging.info("No NEW offers found matching criteria. Notification file will not be created.")
        return False # Indicate file was not created/needed

    # If we reach here, there are new offers to write
    logging.info(f"Saving {len(offers)} NEW offers to notification file '{filename}'...")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"--- NEW FunPay Offers (Price < ${max_price:.2f}, SP >= {min_sp:.1f}M) ---\n")
            f.write(f"--- Source: {url} ---\n\n")
            for i, offer in enumerate(offers):
                f.write(f"Offer #{i+1} (ID: {offer['id']})\n")
                clean_description = ' '.join(offer['description'].split())
                f.write(f"  Desc: {clean_description}\n")
                f.write(f"  Seller: {offer['seller']}\n")
                f.write(f"  Price: {offer['price_text']} (${offer['price_usd']:.2f})\n")
                f.write(f"  SP (M): {offer['sp_million']:.1f}M\n")
                # Ensure link is properly constructed if href was stored
                offer_link = offer.get('href', f"https://funpay.com/en/lots/offer?id={offer['id']}")
                f.write(f"  Link: {offer_link}\n")
                f.write("-" * 20 + "\n\n")
        logging.info(f"Successfully saved NEW offers to {filename}")
        return True # Indicate file was created
    except IOError as e:
        logging.error(f"Error writing new offers file '{filename}': {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred writing {filename}: {e}")
    return False # Indicate file creation failed

# --- Main Execution Logic ---
if __name__ == "__main__":
    start_time = time.time()
    logging.info("="*30)
    logging.info("Starting Funpay scraper script with ID tracking...")

    # 1. Load previously processed IDs
    processed_ids = load_processed_ids(PROCESSED_IDS_FILE)

    # 2. Scrape and filter, getting only NEW offers and their IDs
    logging.info(f"Filtering for: Price < ${MAX_PRICE_USD:.2f} AND SP >= {MIN_SP_MILLION:.1f}M")
    new_offers_list, new_ids_found_this_run = scrape_funpay_offers(URL, MAX_PRICE_USD, MIN_SP_MILLION, processed_ids)

    # 3. Save the NEW offers to the notification file (if any were found)
    # This function now only creates the file if new_offers_list is not empty
    notification_file_created = save_new_offers_to_file(
        new_offers_list,
        OFFERS_OUTPUT_FILE,
        MAX_PRICE_USD,
        MIN_SP_MILLION,
        URL
    )

    # 4. If new offers were found AND successfully saved, update the persistent processed IDs file
    if notification_file_created and new_ids_found_this_run:
        append_processed_ids(PROCESSED_IDS_FILE, new_ids_found_this_run)
    elif not notification_file_created and new_ids_found_this_run:
         # This case should ideally not happen if save_new_offers_to_file works correctly
         logging.warning("New offers were identified, but saving to notification file failed. Processed IDs file will NOT be updated this run to avoid missing notifications later.")
    elif not new_ids_found_this_run:
         logging.info("No new offer IDs to append to processed list.")


    end_time = time.time()
    logging.info(f"Script finished in {end_time - start_time:.2f} seconds.")
    logging.info("="*30)
