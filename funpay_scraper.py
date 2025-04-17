import requests
from bs4 import BeautifulSoup
import time
import logging
import re
import os # Needed for file existence check
from urllib.parse import urlparse, parse_qs # Better way to get URL parameters

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OFFERS_OUTPUT_FILE = "offers.txt" # File for current new offers
PROCESSED_IDS_FILE = "processed_ids.txt" # File to store IDs already seen
MAX_PRICE_USD = 50.00
MIN_SP_MILLION = 10.0

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9',
    'Referer': 'https://funpay.com/en/',
}
REQUEST_DELAY_SECONDS = 2
REQUEST_TIMEOUT = 20

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Functions ---

def load_processed_ids(filename):
    """Loads previously processed offer IDs from a file into a set."""
    processed_ids = set()
    if not os.path.exists(filename):
        logging.info(f"'{filename}' not found. Assuming first run or no previous IDs.")
        return processed_ids
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.isdigit(): # Ensure it's a valid ID format
                    processed_ids.add(line)
        logging.info(f"Loaded {len(processed_ids)} processed IDs from '{filename}'.")
    except IOError as e:
        logging.error(f"Error reading processed IDs file '{filename}': {e}")
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


def extract_offer_id_from_href(href):
    """Extracts the offer ID from the 'id' query parameter of a URL."""
    if not href:
        return None
    try:
        parsed_url = urlparse(href)
        query_params = parse_qs(parsed_url.query)
        # parse_qs returns a list for each param, get the first element
        offer_id = query_params.get('id', [None])[0]
        if offer_id and offer_id.isdigit():
            return offer_id
    except Exception as e:
        logging.warning(f"Could not parse offer ID from href '{href}': {e}")
    return None

def extract_sp_from_description(description):
    """Attempts to extract Skill Points (in millions) from the description text."""
    # (Keep the existing SP extraction logic here - no changes needed)
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
            if 'k sp' in potential_k_context or 'k ' in potential_k_context or \
               (match.start(1) > 0 and description_lower[match.start(1)-1:match.start(1)] == 'k'):
                 logging.debug(f"Found 'k' near SP match in '{description[:50]}...', likely not millions. Skipping.")
                 continue
            try:
                sp_value = float(match.group(1))
                logging.debug(f"Extracted SP value: {sp_value} from description using pattern: '{pattern}'")
                return sp_value
            except (ValueError, IndexError):
                logging.warning(f"Could not convert extracted SP '{match.group(1)}' to float.")
                continue
    logging.debug(f"Could not find SP value in description: '{description[:50]}...'")
    return None


def scrape_funpay_offers(url, max_price, min_sp, processed_ids_set):
    """
    Scrapes offers, filters by price/SP, checks against processed IDs,
    and returns a list of NEW matching offers AND their IDs.
    """
    logging.info(f"Attempting to fetch URL: {url}")
    new_matching_offers = []
    newly_found_ids = set() # Keep track of IDs found in *this* run that meet criteria

    try:
        # ... (request sending and parsing logic remains the same) ...
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds before request...")
        time.sleep(REQUEST_DELAY_SECONDS)
        logging.info(f"Sending GET request")
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        logging.info(f"Received response with status code: {response.status_code}")
        response.raise_for_status()
        logging.info("Successfully fetched page content.")
        soup = BeautifulSoup(response.text, 'html.parser')
        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers.")

        if not offer_containers:
             # ... (error handling for no containers) ...
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

            # --- Extract other details (Description, Seller, Price) ---
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
                # ... (price parsing logic remains the same) ...
                price_match = re.search(r'[\$€£]?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)', price_text.replace(',', ''))
                if price_match: price_usd = float(price_match.group(1))
                else:
                    fallback_match = re.search(r'(\d+\.?\d*)', price_text)
                    if fallback_match: price_usd = float(fallback_match.group(1))
                    else: raise ValueError(f"No numeric price found: '{price_text}'")
            except ValueError as ve:
                logging.error(f"Offer ID {offer_id}: Could not convert price '{price_text}'. Error: {ve}. Skipping.")
                continue
            except Exception as e:
                 logging.error(f"Offer ID {offer_id}: Unexpected error processing price: {e}. Skipping.")
                 continue

            # --- Extract SP ---
            extracted_sp = extract_sp_from_description(description)

            # --- Filtering Logic ---
            price_ok = price_usd is not None and price_usd < max_price
            sp_ok = extracted_sp is not None and extracted_sp >= min_sp

            if price_ok and sp_ok:
                eligible_count += 1
                # --- Check against processed IDs ---
                if offer_id not in processed_ids_set:
                    new_count += 1
                    logging.info(f"--> NEW MATCH FOUND: Offer ID {offer_id}, Price ${price_usd:.2f}, SP {extracted_sp:.1f}M")
                    offer_data = {
                        'id': offer_id, # Store the ID
                        'description': description,
                        'seller': seller,
                        'price_usd': price_usd,
                        'price_text': price_text,
                        'sp_million': extracted_sp
                    }
                    new_matching_offers.append(offer_data)
                    newly_found_ids.add(offer_id) # Add to the set of IDs found *this run*
                else:
                    logging.debug(f"Offer ID {offer_id}: Meets criteria but already processed. Skipping notification.")
            # else: # Optional: Log why it was rejected if needed for debugging
            #     if not price_ok: logging.debug(f"Offer ID {offer_id}: Rejected on price (${price_usd:.2f})")
            #     if not sp_ok: logging.debug(f"Offer ID {offer_id}: Rejected on SP ({extracted_sp}M)")


    # ... (exception handling for requests/parsing remains the same) ...
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")
        return [], set()
    except Exception as e:
        logging.error(f"An unexpected error occurred during scraping: {e}")
        return [], set()

    logging.info(f"Processed {processed_count} offers. Found {eligible_count} meeting Price/SP criteria.")
    logging.info(f"Found {len(new_matching_offers)} NEW offers (not previously processed).")
    return new_matching_offers, newly_found_ids


def save_new_offers_to_file(offers, filename, max_price, min_sp, url):
    """
    Saves the list of NEW filtered offers to a text file ONLY IF offers are found.
    """
    if not offers:
        logging.info("No NEW offers found matching criteria. Skipping file creation for notification.")
        return False # Indicate file was not created

    logging.info(f"Saving {len(offers)} NEW offers to {filename}...")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"--- NEW Offers matching Price < ${max_price:.2f} AND SP >= {min_sp:.1f}M from {url} ---\n\n")
            for i, offer in enumerate(offers):
                f.write(f"Offer #{i+1} (ID: {offer['id']})\n") # Include ID in output
                clean_description = ' '.join(offer['description'].split())
                f.write(f"  Description: {clean_description}\n")
                f.write(f"  Seller: {offer['seller']}\n")
                f.write(f"  Price: {offer['price_text']} (${offer['price_usd']:.2f})\n")
                f.write(f"  SP (Millions, extracted): {offer['sp_million']:.1f}M\n")
                f.write(f"  Link: https://funpay.com/en/lots/offer?id={offer['id']}\n") # Add link
                f.write("-" * 20 + "\n\n")
        logging.info(f"Successfully saved NEW offers to {filename}")
        return True # Indicate file was created
    except IOError as e:
        logging.error(f"Error writing new offers file {filename}: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during file writing: {e}")
    return False # Indicate file was not created on error

# --- Main Execution ---
if __name__ == "__main__":
    logging.info("Starting Funpay scraper script with ID tracking...")

    # Load previously processed IDs
    processed_ids = load_processed_ids(PROCESSED_IDS_FILE)

    logging.info(f"Filtering for: Price < ${MAX_PRICE_USD:.2f} AND SP >= {MIN_SP_MILLION:.1f}M")
    # Scrape and filter, getting only NEW offers and their IDs
    new_offers_list, new_ids_found = scrape_funpay_offers(URL, MAX_PRICE_USD, MIN_SP_MILLION, processed_ids)

    # Save the NEW offers to the notification file (if any)
    file_created = save_new_offers_to_file(new_offers_list, OFFERS_OUTPUT_FILE, MAX_PRICE_USD, MIN_SP_MILLION, URL)

    # If new offers were found and saved, update the processed IDs file
    if file_created and new_ids_found:
        append_processed_ids(PROCESSED_IDS_FILE, new_ids_found)
    elif not file_created and new_ids_found:
         logging.warning("New offers were found, but saving to notification file failed. Processed IDs file will NOT be updated this run.")


    logging.info("Script finished.")
