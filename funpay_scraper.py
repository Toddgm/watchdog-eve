import requests
from bs4 import BeautifulSoup
import time
import logging
import re # Import regex module

# --- Configuration ---
URL = "https://funpay.com/en/lots/687/"
OUTPUT_FILE = "offers.txt"
MAX_PRICE_USD = 50.00
MIN_SP_MILLION = 10.0 # Minimum Skill Points required (in Millions)

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
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Functions ---

def extract_sp_from_description(description):
    """
    Attempts to extract Skill Points (in millions) from the description text.
    Returns float SP value if found, otherwise None.
    Assumes numbers near 'sp' refer to millions unless 'k' is present.
    """
    # Case-insensitive search
    description_lower = description.lower()

    # Regex explanation:
    # (\d+(?:\.\d+)?) : Capture group 1: one or more digits, optionally followed by a decimal point and more digits (captures integers and floats)
    # \s*             : Zero or more whitespace characters
    # (?:m|mil|million)? : Optional non-capturing group for "m", "mil", or "million" (allows "150m sp", "150 sp", "150 million sp")
    # \s*             : Zero or more whitespace characters
    # sp              : The literal characters "sp"
    # Lookbehind (?<!k) : Ensure the match isn't preceded by 'k' (to avoid matching "500k sp" as 500 million)
    # Using word boundaries (\b) might also help isolate the number/sp combo
    # Let's try a slightly simpler regex first, focusing on number followed by optional M and SP
    # pattern = r'(\d+(?:\.\d+)?)\s*(?:m|mil|million)?\s*sp'

    # More robust pattern: Look for SP and then backtrack slightly or lookahead for a number
    # This is complex. Let's target common patterns first: "NUMBER m sp", "NUMBER sp", "sp NUMBER m"
    patterns = [
        r'(\d+(?:\.\d+)?)\s*(?:m|mil|million)\s*sp', # e.g., "150m sp", "150 million sp"
        r'sp\s*(\d+(?:\.\d+)?)\s*(?:m|mil|million)', # e.g., "sp 150m", "sp: 150 million"
        r'(\d+(?:\.\d+)?)\s*sp'                     # e.g., "150 sp" (ASSUMES millions) - place last as less specific
    ]

    for pattern in patterns:
        match = re.search(pattern, description_lower)
        if match:
            # Check if 'k' (for thousand) is nearby, indicating it's NOT millions
            # Simple check: look for 'k' immediately before the number or after sp
            # A better check might involve word tokenization, but let's keep it regex-based
            potential_k_context = description_lower[max(0, match.start()-5):min(len(description_lower), match.end()+5)]
            if 'k sp' in potential_k_context or 'k ' in potential_k_context or \
               description_lower[match.start(1)-1:match.start(1)] == 'k': # check char before number
                 logging.debug(f"Found 'k' near SP match in '{description[:50]}...', likely not millions. Skipping this pattern match.")
                 continue # Skip this match if 'k' seems present

            try:
                sp_value = float(match.group(1))
                logging.debug(f"Extracted SP value: {sp_value} from description using pattern: '{pattern}'")
                return sp_value
            except (ValueError, IndexError):
                logging.warning(f"Could not convert extracted SP '{match.group(1)}' to float.")
                continue # Try next pattern if conversion fails

    logging.debug(f"Could not find SP value in description: '{description[:50]}...'")
    return None # Return None if no pattern matched or 'k' was detected


def scrape_funpay_offers(url, max_price, min_sp):
    """
    Scrapes offers from the Funpay URL, filters by price AND SP, and returns a list of matching offers.
    """
    logging.info(f"Attempting to fetch URL: {url}")
    filtered_offers = []

    try:
        logging.info(f"Waiting {REQUEST_DELAY_SECONDS} seconds before request...")
        time.sleep(REQUEST_DELAY_SECONDS)

        logging.info(f"Sending GET request with headers") # Removed headers from log for brevity
        response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        logging.info(f"Received response with status code: {response.status_code}")
        response.raise_for_status()
        logging.info("Successfully fetched page content.")

        soup = BeautifulSoup(response.text, 'html.parser')

        offer_containers = soup.find_all('a', class_='tc-item')
        logging.info(f"Found {len(offer_containers)} potential offer containers using selector: ('a', class_='tc-item')")

        if not offer_containers:
            # ... (error handling for no containers found remains the same) ...
             body_text = soup.body.get_text(strip=True) if soup.body else ""
             if "checking your browser" in body_text.lower() or "enable javascript" in body_text.lower():
                 logging.error("Page seems to require JavaScript or is performing a browser check. requests/BeautifulSoup cannot handle this directly.")
             elif not body_text:
                 logging.warning("Response body seems empty after parsing.")
             else:
                 logging.warning("No offer containers found matching the specified selector ('a', class_='tc-item'). The website structure might have changed again, or the page content is different than expected.")
             return []


        processed_count = 0
        kept_count = 0
        for container in offer_containers:
            processed_count += 1
            # Extract Description
            desc_tag = container.find('div', class_='tc-desc-text')
            description = desc_tag.get_text(separator=' ', strip=True) if desc_tag else "N/A"

            # Extract Seller
            seller_tag = container.find('div', class_='media-user-name')
            seller = seller_tag.get_text(strip=True) if seller_tag else "N/A"

            # Extract Price
            price_container_tag = container.find('div', class_='tc-price')
            price_text = price_container_tag.get_text(strip=True) if price_container_tag else None

            if not price_text:
                logging.warning(f"Could not find price text for an offer. Skipping. Description: {description[:50]}...")
                continue

            # --- Process Price ---
            price_usd = None
            try:
                price_match = re.search(r'[\$€£]?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)', price_text.replace(',', ''))
                if price_match:
                    price_str = price_match.group(1)
                    price_usd = float(price_str)
                else:
                    fallback_match = re.search(r'(\d+\.?\d*)', price_text)
                    if fallback_match:
                        price_str = fallback_match.group(1)
                        price_usd = float(price_str)
                        logging.warning(f"Used fallback regex for price: '{price_text}' -> {price_usd}")
                    else:
                         raise ValueError(f"No numeric price found in text: '{price_text}'")

            except ValueError as ve:
                logging.error(f"Could not convert price text '{price_text}' to float. Skipping offer. Error: {ve}. Desc: {description[:50]}...")
                continue # Skip offer if price cannot be parsed
            except Exception as e:
                 logging.error(f"Unexpected error processing price for offer: {e}. Skipping offer. Desc: {description[:50]}...")
                 continue # Skip offer on unexpected price error

            # --- Extract SP from Description ---
            extracted_sp = extract_sp_from_description(description) # Returns float (millions) or None

            # --- Filtering Logic ---
            if price_usd is not None and price_usd < max_price:
                if extracted_sp is not None and extracted_sp >= min_sp:
                    offer_data = {
                        'description': description,
                        'seller': seller,
                        'price_usd': price_usd,
                        'price_text': price_text,
                        'sp_million': extracted_sp # Store the extracted SP
                    }
                    filtered_offers.append(offer_data)
                    kept_count += 1
                    logging.debug(f"KEPT Offer: Price ${price_usd:.2f} (< ${max_price:.2f}), SP {extracted_sp:.1f}M (>= {min_sp:.1f}M)")
                else:
                     logging.debug(f"REJECTED Offer (SP criteria): Price ${price_usd:.2f}, SP {extracted_sp}M (Required >= {min_sp:.1f}M). Desc: {description[:50]}...")
            else:
                logging.debug(f"REJECTED Offer (Price criteria): Price ${price_usd:.2f} (Required < ${max_price:.2f}). Desc: {description[:50]}...")


    # ... (exception handling remains the same) ...
    except requests.exceptions.Timeout:
        logging.error(f"Request timed out after {REQUEST_TIMEOUT} seconds.")
        return []
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP Error: {e.response.status_code} {e.response.reason} for URL: {url}")
        if 400 <= e.response.status_code < 500:
             logging.error("Client error - check headers, URL, or potentially IP blocking.")
        return []
    except requests.exceptions.RequestException as e:
        logging.error(f"Request failed: {e}")
        return []
    except AttributeError as e:
         logging.error(f"AttributeError during parsing: {e}. Check selectors against website structure.")
         return []
    except Exception as e:
        logging.error(f"An unexpected error occurred during scraping: {e}")
        return []

    logging.info(f"Processed {processed_count} offers. Found {len(filtered_offers)} offers matching Price < ${max_price:.2f} AND SP >= {min_sp:.1f}M.")
    return filtered_offers

def save_offers_to_file(offers, filename, max_price, min_sp):
    """
    Saves the list of filtered offers to a text file.
    """
    logging.info(f"Saving {len(offers)} offers to {filename}...")
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"--- Offers matching Price < ${max_price:.2f} AND SP >= {min_sp:.1f}M from {URL} ---\n\n")
            if not offers:
                f.write("No offers found matching the specified criteria.\n")
                f.write("(Note: SP extraction relies on parsing description text and may not find all matches.)\n")
                logging.info("Output file written (no offers found).")
                return

            for i, offer in enumerate(offers):
                f.write(f"Offer #{i+1}\n")
                clean_description = ' '.join(offer['description'].split())
                f.write(f"  Description: {clean_description}\n")
                f.write(f"  Seller: {offer['seller']}\n")
                f.write(f"  Price: {offer['price_text']} (${offer['price_usd']:.2f})\n")
                f.write(f"  SP (Millions, extracted): {offer['sp_million']:.1f}M\n") # Added SP to output
                f.write("-" * 20 + "\n\n")
        logging.info(f"Successfully saved offers to {filename}")
    except IOError as e:
        logging.error(f"Error writing to file {filename}: {e}")
    except Exception as e:
        logging.error(f"An unexpected error occurred during file writing: {e}")

# --- Main Execution ---
if __name__ == "__main__":
    logging.info("Starting Funpay scraper script...")
    logging.info(f"Filtering for: Price < ${MAX_PRICE_USD:.2f} AND SP >= {MIN_SP_MILLION:.1f}M")
    filtered_offers_list = scrape_funpay_offers(URL, MAX_PRICE_USD, MIN_SP_MILLION)
    save_offers_to_file(filtered_offers_list, OUTPUT_FILE, MAX_PRICE_USD, MIN_SP_MILLION)
    logging.info("Script finished.")
