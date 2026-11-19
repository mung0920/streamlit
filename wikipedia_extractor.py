import os
import re
import sys
from datetime import datetime
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = "https://en.wikipedia.org"
ARTICLE_URL = BASE_URL + "/wiki/"
SEARCH_API = BASE_URL + "/w/api.php"
HEADERS = {"User-Agent": "ISOM3400-Student-Extractor/1.0 (educational use only)"}
REQUEST_TIMEOUT = 10          # seconds
MAX_RETRIES = 3
LICENCE = "CC BY-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
CITATION_RE = re.compile(r"\[\d+\]")          # matches [1], [23] ...


# --------------------------------------------------------------------------
# Custom exceptions
# --------------------------------------------------------------------------
class ArticleNotFoundError(Exception):
    """Raised when Wikipedia has no article for the requested topic."""


class FetchError(Exception):
    """Raised when a page cannot be downloaded from Wikipedia."""


class ExtractionError(Exception):
    """Raised when the page structure cannot be parsed as expected."""


# --------------------------------------------------------------------------
# Networking helpers
# --------------------------------------------------------------------------
def fetch_page(url):
    """Download a URL and return a parsed BeautifulSoup document.

    Retries on transient network errors. Returns None for a 404.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return BeautifulSoup(response.text, "html.parser")
        except requests.exceptions.HTTPError as exc:
            last_error = exc
            break
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            last_error = exc
            print(f"  Network problem (attempt {attempt}/{MAX_RETRIES}): {exc}")
        except requests.exceptions.RequestException as exc:
            last_error = exc
            break
    raise FetchError(f"Could not download page: {last_error}")


def resolve_topic(topic):
    """Return the best-matching Wikipedia article title, or None."""
    params = {
        "action": "query",
        "list": "search",
        "srsearch": topic,
        "srlimit": 1,
        "format": "json",
    }
    try:
        response = requests.get(SEARCH_API, params=params, headers=HEADERS,
                                timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        raise FetchError(f"Search request failed: {exc}")
    except ValueError as exc:
        raise FetchError(f"Wikipedia returned an unreadable response: {exc}")

    hits = data.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


# --------------------------------------------------------------------------
# Heading utilities
# --------------------------------------------------------------------------
def _heading_level(element):
    """Return the numeric level of a heading or its wrapper, else None.

    Handles both the classic <h2> tags and Wikipedia's newer
    <div class="mw-heading mw-heading2"> wrappers.
    """
    if element.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return int(element.name[1])
    if element.name == "div" and element.get("class"):
        for cls in element["class"]:
            match = re.fullmatch(r"mw-heading(\d)", cls)
            if match:
                return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# The article class
# --------------------------------------------------------------------------
class WikipediaArticle:
    """Represents a parsed Wikipedia article and its sections."""

    def __init__(self, title, url, soup):
        self.title = title
        self.url = url
        self.soup = soup
        # Scope all scraping to the article body.
        self.content = soup.find("div", {"id": "mw-content-text"}) or soup
        self.sections = self._collect_sections()

    @classmethod
    def load(cls, topic):
        """Resolve a topic and build a WikipediaArticle, or raise."""
        title = resolve_topic(topic)
        if not title:
            raise ArticleNotFoundError(topic)
        url = ARTICLE_URL + quote(title.replace(" ", "_"))
        soup = fetch_page(url)
        if soup is None:
            raise ArticleNotFoundError(topic)
        if soup.find("div", {"id": "mw-content-text"}) is None:
            raise ExtractionError("Unexpected page layout (no article body).")
        return cls(title, url, soup)

    # ---- text cleaning ----------------------------------------------------
    @staticmethod
    def _clean_text(tag):
        """Return tag text with inline citation markers and extra spaces removed."""
        text = tag.get_text()
        text = CITATION_RE.sub("", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ---- section discovery ------------------------------------------------
    def _collect_sections(self):
        """Return a list of dicts describing the top-level sections."""
        sections = []
        for heading in self.content.find_all("h2"):      # find_all() in use
            title = self._clean_text(heading)
            if not title:
                continue
            sections.append({"number": len(sections) + 1,
                             "title": title,
                             "heading": heading})
        return sections

    def list_sections(self):
        """Return the section titles in order."""
        return [s["title"] for s in self.sections]

    def get_section(self, number):
        """Return the section dict for a given number, or None."""
        for section in self.sections:
            if section["number"] == number:
                return section
        return None

    # ---- extraction -------------------------------------------------------
    @staticmethod
    def _heading_container(heading):
        """Return the wrapper div of a heading, if there is one."""
        parent = heading.parent
        if parent is not None and parent.name == "div" and parent.get("class"):
            if any(c.startswith("mw-heading") for c in parent["class"]):
                return parent
        return heading

    def _format_list(self, list_tag):
        """Render a <ul>/<ol> as plain-text bullet or numbered lines."""
        lines = []
        ordered = list_tag.name == "ol"
        index = 1
        for item in list_tag.find_all("li", recursive=False):
            text = self._clean_text(item)
            if not text:
                continue
            marker = f"{index}." if ordered else "-"
            lines.append(f"  {marker} {text}")
            index += 1
        lines.append("")
        return lines

    def _format_table(self, table_tag):
        """Render a <table> as rows of pipe-separated cells."""
        lines = ["TABLE:"]
        for row in table_tag.find_all("tr"):             # find_all() in use
            cells = row.find_all(["th", "td"])           # find_all() in use
            values = [self._clean_text(cell) for cell in cells]
            values = [v for v in values if v]
            if values:
                lines.append("  | " + " | ".join(values) + " |")
        lines.append("")
        return lines

    def extract_section(self, section):
        """Return a formatted text block for a single section."""
        lines = ["=" * 70, f"SECTION: {section['title']}", "=" * 70, ""]

        heading = section["heading"]
        try:
            level = int(heading.name[1])
        except (ValueError, TypeError, IndexError) as exc:
            raise ExtractionError(f"Bad heading tag: {exc}")

        container = self._heading_container(heading)
        found_any = False

        for element in container.find_next_siblings():
            elem_level = _heading_level(element)

            # Stop at the next heading of the same or higher level.
            if elem_level is not None and elem_level <= level:
                break

            # Lower-level heading => a subheading inside this section.
            if elem_level is not None:
                lines += [f"--- {self._clean_text(element)} ---", ""]
                continue

            if element.name == "p":                      # find() style access
                text = self._clean_text(element)
                if text:
                    lines += [text, ""]
                    found_any = True
            elif element.name in ("ul", "ol"):
                lines.extend(self._format_list(element))
                found_any = True
            elif element.name == "table":
                lines.extend(self._format_table(element))
                found_any = True

        if not found_any:
            lines += ["(No extractable content found in this section.)", ""]

        return "\n".join(lines)

    def extract_sections(self, numbers):
        """Extract and concatenate several sections by number."""
        blocks = []
        for number in numbers:
            section = self.get_section(number)
            if section:
                blocks.append(self.extract_section(section))
        if not blocks:
            raise ExtractionError("None of the chosen sections could be read.")
        return "\n".join(blocks)


# --------------------------------------------------------------------------
# Selection parsing and validation
# --------------------------------------------------------------------------
def parse_selection(text, max_number):
    """Convert a selection string into a sorted, validated list of numbers.

    Supports single numbers ("3"), lists ("1,4,7"), ranges ("2-5"),
    combinations ("1, 3-5"), and "all".
    """
    if not text or not text.strip():
        raise ValueError("Nothing was entered.")
    cleaned = text.strip().lower()
    if cleaned == "all":
        return list(range(1, max_number + 1))

    numbers = set()
    for token in cleaned.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-")
            if (len(parts) != 2 or not parts[0].strip().isdigit()
                    or not parts[1].strip().isdigit()):
                raise ValueError(f"'{token}' is not a valid range.")
            start, end = int(parts[0]), int(parts[1])
            if start > end:
                raise ValueError(f"Range '{token}' runs backwards.")
            numbers.update(range(start, end + 1))
        elif token.isdigit():
            numbers.add(int(token))
        else:
            raise ValueError(f"'{token}' is not a valid section number.")

    if not numbers:
        raise ValueError("No section numbers were found.")

    for number in numbers:
        if number < 1 or number > max_number:
            raise ValueError(f"Section {number} is out of range (1-{max_number}).")

    return sorted(numbers)


# --------------------------------------------------------------------------
# File handling
# --------------------------------------------------------------------------
def build_filename(title):
    """Create a safe, meaningful .txt filename from the article title."""
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"\s+", "_", safe)
    if not safe:
        safe = "wikipedia_extract"
    return safe[:80] + ".txt"


def build_header(article, numbers):
    """Build the required file header (URL, timestamp, licence)."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    titles = ", ".join(article.get_section(n)["title"] for n in numbers)
    return "\n".join([
        "Wikipedia Article Extractor",
        "=" * 70,
        f"Article title : {article.title}",
        f"Source URL    : {article.url}",
        f"Extracted on  : {stamp}",
        f"Sections      : {titles}",
        f"Licence       : {LICENCE} ({LICENCE_URL})",
        "=" * 70,
        "",
        "This content is reused from Wikipedia and is available under the",
        "Creative Commons Attribution-ShareAlike 4.0 licence.",
        "",
    ])


def choose_output_path(filename):
    """Return a safe path to write to, prompting the user on collisions."""
    if not os.path.exists(filename):
        return filename
    base, ext = os.path.splitext(filename)
    while True:
        choice = input(
            f"The file '{filename}' already exists. "
            "[O]verwrite, [N]ew name, [C]ancel: "
        ).strip().lower()
        if choice in ("o", "overwrite"):
            return filename
        if choice in ("n", "new"):
            counter = 1
            while True:
                candidate = f"{base}({counter}){ext}"
                if not os.path.exists(candidate):
                    return candidate
                counter += 1
        if choice in ("c", "cancel"):
            return None
        print("  Invalid choice. Please type O, N or C.")


def save_to_file(path, content):
    """Write content to path, raising a friendly error on failure."""
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        raise IOError(f"Could not write the file: {exc}") from exc


# --------------------------------------------------------------------------
# User interface
# --------------------------------------------------------------------------
def print_welcome():
    print("=" * 70)
    print(" Welcome to the Wikipedia Article Extractor")
    print("=" * 70)
    print("This console app lets you search Wikipedia, browse an article's")
    print("top-level sections, choose the ones you want, and save a clean,")
    print("readable extract to a .txt file.")
    print()
    print("How to use it:")
    print("  1. Type a topic to search for.")
    print("  2. View the list of available sections.")
    print("  3. Pick sections using numbers, lists (1,4), ranges (2-5)")
    print("     or the word 'all'.")
    print("  4. Save the extract to a .txt file in this folder.")
    print("  0. Exit at any time.")
    print("=" * 70)
    print()


def print_menu(article):
    print("-" * 70)
    label = article.title if article else "(none loaded)"
    print(f"Current article: {label}")
    print("  1. Search for a topic")
    print("  2. View available sections")
    print("  3. Extract selected sections")
    print("  4. Save extract to a .txt file")
    print("  0. Exit")
    print("-" * 70)


def prompt_topic():
    """Prompt for a topic and load the article, with retry on failure."""
    while True:
        topic = input("Enter a topic to search for (or 'back' to return): ").strip()
        if not topic:
            print("  Please type a topic before pressing Enter.")
            continue
        if topic.lower() == "back":
            return None
        try:
            article = WikipediaArticle.load(topic)
        except ArticleNotFoundError:
            print(f"  No Wikipedia article found for '{topic}'. Please try again.")
            continue
        except FetchError as exc:
            print(f"  Problem reaching Wikipedia: {exc}")
            continue
        except ExtractionError as exc:
            print(f"  Could not read the article: {exc}")
            continue
        print(f"\n  Loaded: {article.title}")
        print(f"  URL   : {article.url}\n")
        return article


def show_sections(article):
    print("\nAvailable sections:")
    for section in article.sections:
        print(f"  {section['number']:>2}. {section['title']}")
    print()


def choose_sections(article):
    """Prompt for a section selection, re-prompting until it is valid."""
    while True:
        show_sections(article)
        raw = input(
            "Select sections (e.g. 1, 3-5, all; 'back' to cancel): "
        ).strip()
        if raw.lower() == "back":
            return None
        try:
            return parse_selection(raw, len(article.sections))
        except ValueError as exc:
            print(f"  Invalid selection: {exc}")


def main():
    """Console-based user interface."""
    print_welcome()
    article = None
    extracted_text = None
    extracted_numbers = None

    while True:
        print_menu(article)
        choice = input("Your choice: ").strip().lower()

        if choice in ("0", "q", "exit", "quit"):
            print("\nThank you for using the Wikipedia Article Extractor. Goodbye!")
            break

        elif choice == "1":
            new_article = prompt_topic()
            if new_article:
                article, extracted_text, extracted_numbers = new_article, None, None

        elif choice == "2":
            if article is None:
                print("  Please search for a topic first (option 1).")
            else:
                show_sections(article)

        elif choice == "3":
            if article is None:
                print("  Please search for a topic first (option 1).")
                continue
            if not article.sections:
                print("  This article has no sections to extract.")
                continue
            numbers = choose_sections(article)
            if numbers is None:
                continue
            try:
                extracted_text = article.extract_sections(numbers)
                extracted_numbers = numbers
            except ExtractionError as exc:
                print(f"  Extraction failed: {exc}")
                continue
            titles = ", ".join(article.get_section(n)["title"] for n in numbers)
            print(f"\n  Extracted {len(numbers)} section(s): {titles}\n")

        elif choice == "4":
            if not extracted_text:
                print("  Nothing to save yet. Extract some sections first (option 3).")
                continue
            path = choose_output_path(build_filename(article.title))
            if path is None:
                print("  Save cancelled.")
                continue
            full_content = build_header(article, extracted_numbers) + "\n" + extracted_text
            try:
                save_to_file(path, full_content)
            except IOError as exc:
                print(f"  {exc}")
                continue
            print(f"  Saved to: {os.path.abspath(path)}\n")

        else:
            print("  Invalid choice. Please enter 1, 2, 3, 4 or 0.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted. Goodbye!")
        sys.exit(0)
