"""
wikipedia_extractor.py  (Streamlit web version)

Searches Wikipedia, lists an article's top-level sections, lets the user
choose sections, extracts clean text (paragraphs, lists, tables,
subheadings) and offers the result as a downloadable .txt file.

Run locally with:  streamlit run wikipedia_extractor.py
"""

import re
from datetime import datetime
from urllib.parse import quote

import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
BASE_URL = "https://en.wikipedia.org"
ARTICLE_URL = BASE_URL + "/wiki/"
SEARCH_API = BASE_URL + "/w/api.php"
HEADERS = {"User-Agent": "ISOM3400-Student-Extractor/1.0 (educational use only)"}
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
LICENCE = "CC BY-SA 4.0"
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
CITATION_RE = re.compile(r"\[\d+\]")

st.set_page_config(page_title="Wikipedia Article Extractor", page_icon="📚")


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
    """Return the numeric level of a heading or its wrapper, else None."""
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

    @staticmethod
    def _clean_text(tag):
        text = tag.get_text()
        text = CITATION_RE.sub("", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _collect_sections(self):
        sections = []
        for heading in self.content.find_all("h2"):
            title = self._clean_text(heading)
            if not title:
                continue
            sections.append({"number": len(sections) + 1,
                             "title": title,
                             "heading": heading})
        return sections

    def get_section(self, number):
        for section in self.sections:
            if section["number"] == number:
                return section
        return None

    @staticmethod
    def _heading_container(heading):
        parent = heading.parent
        if parent is not None and parent.name == "div" and parent.get("class"):
            if any(c.startswith("mw-heading") for c in parent["class"]):
                return parent
        return heading

    def _format_list(self, list_tag):
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
        lines = ["TABLE:"]
        for row in table_tag.find_all("tr"):
            cells = row.find_all(["th", "td"])
            values = [self._clean_text(cell) for cell in cells]
            values = [v for v in values if v]
            if values:
                lines.append("  | " + " | ".join(values) + " |")
        lines.append("")
        return lines

    def extract_section(self, section):
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
            if elem_level is not None and elem_level <= level:
                break
            if elem_level is not None:
                lines += [f"--- {self._clean_text(element)} ---", ""]
                continue
            if element.name == "p":
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
        blocks = []
        for number in numbers:
            section = self.get_section(number)
            if section:
                blocks.append(self.extract_section(section))
        if not blocks:
            raise ExtractionError("None of the chosen sections could be read.")
        return "\n".join(blocks)


# --------------------------------------------------------------------------
# Selection validation and file naming
# --------------------------------------------------------------------------
def parse_selection(text, max_number):
    """Convert a selection string into a sorted, validated list of numbers."""
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


def build_filename(title):
    safe = re.sub(r"[^\w\s-]", "", title).strip()
    safe = re.sub(r"\s+", "_", safe)
    if not safe:
        safe = "wikipedia_extract"
    return safe[:80] + ".txt"


def build_header(article, numbers):
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


# --------------------------------------------------------------------------
# Streamlit interface
# --------------------------------------------------------------------------
def init_state():
    for key in ("article", "extracted_text", "extracted_numbers"):
        if key not in st.session_state:
            st.session_state[key] = None


init_state()

st.title("📚 Wikipedia Article Extractor")
st.write(
    "Search Wikipedia, browse an article's top-level sections, choose the "
    "ones you want, and download a clean, readable .txt file."
)

topic = st.text_input("Enter a topic to search for", key="topic_box")

if st.button("Search"):
    if not topic.strip():
        st.warning("Please type a topic before searching.")
    else:
        with st.spinner("Searching Wikipedia..."):
            try:
                article = WikipediaArticle.load(topic)
            except ArticleNotFoundError:
                st.error(f"No Wikipedia article found for '{topic}'. "
                         "Please try another topic.")
            except FetchError as exc:
                st.error(f"Problem reaching Wikipedia: {exc}")
            except ExtractionError as exc:
                st.error(f"Could not read the article: {exc}")
            else:
                st.session_state.article = article
                st.session_state.extracted_text = None
                st.session_state.extracted_numbers = None
                st.success(f"Loaded: {article.title}")

article = st.session_state.article

if article is not None:
    st.markdown(f"**Article:** {article.title}")
    st.markdown(f"**Source URL:** {article.url}")

    st.subheader("Available sections")
    for section in article.sections:
        st.write(f"{section['number']}. {section['title']}")

    selection = st.text_input(
        "Select sections to extract (e.g. 1, 3-5, or all)",
        key="selection_box",
    )

    if st.button("Extract sections"):
        try:
            numbers = parse_selection(selection, len(article.sections))
        except ValueError as exc:
            st.error(f"Invalid selection: {exc}")
        else:
            try:
                text = article.extract_sections(numbers)
            except ExtractionError as exc:
                st.error(f"Extraction failed: {exc}")
            else:
                st.session_state.extracted_text = text
                st.session_state.extracted_numbers = numbers
                st.success(f"Extracted {len(numbers)} section(s).")

if st.session_state.extracted_text:
    st.subheader("Preview")
    st.text_area("Extracted content", st.session_state.extracted_text, height=300)

    full_content = (build_header(article, st.session_state.extracted_numbers)
                    + "\n" + st.session_state.extracted_text)
    st.download_button(
        label="💾 Download as .txt file",
        data=full_content,
        file_name=build_filename(article.title),
        mime="text/plain",
    )

if st.button("Clear / start over"):
    st.session_state.article = None
    st.session_state.extracted_text = None
    st.session_state.extracted_numbers = None
    st.rerun()
