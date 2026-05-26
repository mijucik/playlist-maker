import requests
from bs4 import BeautifulSoup

# List of URLs to scrape emotions/genres from
urls = [
    'https://en.wikipedia.org/wiki/List_of_music_genres_and_styles',
    'https://www.enchantedlearning.com/wordlist/music-genres.shtml',
    'https://www.enchantedlearning.com/wordlist/emotions.shtml'
]

# Function to scrape genres from Wikipedia
def scrape_wikipedia_genres(url):
    response = requests.get(url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        # Find all <ul> elements inside the page's content that contain the genre names
        genre_tables = soup.find_all('div', {'class': 'div-col'})  # Target divs that hold multiple columns
        
        # Extract and clean up genre names
        genres = []
        for table in genre_tables:
            for genre in table.find_all('li'):
                genre_name = genre.get_text().strip()
                if genre_name:  # Make sure it's not empty
                    genres.append(genre_name)
        return genres
    else:
        print(f"Failed to retrieve {url}: {response.status_code}")
        return []

# Function to scrape word lists from EnchantedLearning
def scrape_enchantedlearning(url):
    response = requests.get(url)
    if response.status_code == 200:
        soup = BeautifulSoup(response.text, 'html.parser')
        # Find all words in list items (<li>) and extract the text
        word_list = soup.find_all('div', {'class': 'wordlist-item'})  # Change this selector if needed
        
        # Extract and clean up word names
        words = [item.get_text().strip() for item in word_list if item.get_text().strip()]
        return words
    else:
        print(f"Failed to retrieve {url}: {response.status_code}")
        return []

# Master list to store all the words
all_words = []

# Scrape from Wikipedia page for music genres
print("Scraping Wikipedia for music genres...")
wikipedia_genres = scrape_wikipedia_genres('https://en.wikipedia.org/wiki/List_of_music_genres_and_styles')
all_words.extend(wikipedia_genres)

# Scrape from EnchantedLearning pages
for url in urls[1:]:
    print(f"Scraping {url}...")
    enchanted_words = scrape_enchantedlearning(url)
    all_words.extend(enchanted_words)

# Remove duplicates (optional)
all_words = list(set(all_words))

# Save the words to a text file
with open('emotions_genres.txt', 'w') as file:
    for word in all_words:
        file.write(f"{word}\n")

print(f"Words successfully saved to 'emotions_genres.txt'. Total words: {len(all_words)}")
