import requests
from bs4 import BeautifulSoup

def scrape_funpay_under_50(url):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "lxml")
        listings = soup.find_all("div", class_="tc-item")
        cheap_offers = []
        
        for listing in listings:
            title_tag = listing.find("div", class_="tc-desc-text")
            title = title_tag.text.strip() if title_tag else "No Title"
            
            price_tag = listing.find("div", class_="tc-price")
            if price_tag:
                price_text = price_tag.text.strip().replace("$", "").strip()
                try:
                    price = float(price_text)
                    if price < 50:
                        cheap_offers.append(f"{title} - ${price:.2f}")
                except ValueError:
                    continue
        
        return cheap_offers
    
    except Exception as e:
        print(f"Error: {e}")
        return []

if __name__ == "__main__":
    target_url = "https://funpay.com/en/lots/687/"
    offers = scrape_funpay_under_50(target_url)
    
    with open("offers.txt", "w") as f:
        if offers:
            f.write("\n".join(offers))
        else:
            f.write("No offers under $50 found.")
