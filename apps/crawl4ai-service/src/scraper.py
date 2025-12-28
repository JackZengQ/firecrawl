"""
Crawl4AI Scraper Wrapper
Provides a clean interface to crawl4ai for the Firecrawl microservice.
"""

import asyncio
import json
import logging
import os
import re
from typing import Optional, List, Dict, Any

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

logger = logging.getLogger("crawl4ai-service.scraper")


def _jsonld_to_markdown(data: Any, depth: int = 0) -> List[str]:
    """
    Convert JSON-LD structured data to readable markdown.
    Preserves all fields for LLM extraction.
    """
    lines = []
    indent = "  " * depth

    if isinstance(data, dict):
        dtype = data.get('@type', '')
        name = data.get('name', '')

        # Create header for typed objects
        if dtype and name:
            lines.append(f"{indent}**{dtype}: {name}**")
        elif dtype:
            lines.append(f"{indent}**{dtype}**")
        elif name:
            lines.append(f"{indent}**{name}**")

        for key, value in data.items():
            # Skip JSON-LD metadata except @type
            if key.startswith('@') or key in ['name']:
                continue

            if isinstance(value, dict):
                if key == 'offers':
                    # Format price inline
                    price = value.get('price', '')
                    currency = value.get('priceCurrency', 'USD')
                    if price:
                        symbol = '$' if currency == 'USD' else f'{currency} '
                        lines.append(f"{indent}- Price: {symbol}{price}")
                else:
                    lines.append(f"{indent}- {key}:")
                    lines.extend(_jsonld_to_markdown(value, depth + 1))
            elif isinstance(value, list):
                # Recursively process arrays (hasMenuSection, hasMenuItem, etc.)
                if key in ['hasMenuSection', 'hasMenuItem', 'itemListElement']:
                    lines.extend(_jsonld_to_markdown(value, depth))
                else:
                    lines.append(f"{indent}- {key}:")
                    lines.extend(_jsonld_to_markdown(value, depth + 1))
            elif value:
                # Output simple values
                lines.append(f"{indent}- {key}: {value}")

        lines.append("")  # Blank line after each object

    elif isinstance(data, list):
        for item in data:
            lines.extend(_jsonld_to_markdown(item, depth))

    return lines


def extract_embedded_json_content(html: str, markdown: str, url: str) -> str:
    """
    Extract structured data from HTML and convert to LLM-friendly markdown.
    Prioritizes JSON-LD (schema.org) data as it's well-structured.
    Preserves all fields to allow LLM to extract what it needs.

    Returns enhanced markdown if extraction was successful, otherwise original markdown.
    """
    # Only process if markdown is minimal relative to HTML size
    # This catches SPA sites where content is in JSON but not rendered to DOM
    if len(html) < 10000:
        return markdown
    if len(markdown) > 50000:
        return markdown
    if len(html) > 0 and len(markdown) / len(html) > 0.05:
        return markdown

    logger.info(f"Attempting JSON extraction for {url} (html={len(html)}, markdown={len(markdown)}, ratio={len(markdown)/len(html) if len(html) > 0 else 0:.4f})")

    extracted_markdown = []

    # Priority 1: JSON-LD structured data (best quality, schema.org format)
    jsonld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>([^<]+)</script>', html)
    for jsonld_str in jsonld_matches:
        try:
            jsonld = json.loads(jsonld_str)
            if isinstance(jsonld, dict):
                dtype = jsonld.get('@type', '')
                # Process Menu, Restaurant, Product, ItemList, etc.
                if dtype in ['Menu', 'Restaurant', 'Product', 'ItemList', 'Recipe', 'Article', 'LocalBusiness']:
                    lines = _jsonld_to_markdown(jsonld)
                    if lines:
                        extracted_markdown.extend(lines)
            elif isinstance(jsonld, list):
                for item in jsonld:
                    if isinstance(item, dict):
                        lines = _jsonld_to_markdown(item)
                        if lines:
                            extracted_markdown.extend(lines)
        except json.JSONDecodeError:
            pass

    # Priority 2: Next.js __NEXT_DATA__ (if no JSON-LD found)
    if not extracted_markdown:
        next_data_match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>([^<]+)</script>', html)
        if next_data_match:
            try:
                next_data = json.loads(next_data_match.group(1))
                # Extract pageProps which usually contains the main data
                page_props = next_data.get('props', {}).get('pageProps', {})
                if page_props:
                    lines = _jsonld_to_markdown(page_props)
                    if lines:
                        extracted_markdown.extend(lines)
            except json.JSONDecodeError:
                pass

    # Build final markdown
    if extracted_markdown:
        # Count meaningful lines (non-empty)
        meaningful_lines = [l for l in extracted_markdown if l.strip()]
        logger.info(f"Extracted {len(meaningful_lines)} lines of structured data for {url}")

        enhanced_md = markdown.rstrip() + "\n\n"
        enhanced_md += "---\n\n"
        enhanced_md += "## Structured Data\n\n"
        enhanced_md += "\n".join(extracted_markdown)

        return enhanced_md

    return markdown

# Configuration
BLOCK_MEDIA = os.getenv("BLOCK_MEDIA", "false").lower() == "true"
PROXY_SERVER = os.getenv("PROXY_SERVER")
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"


class Crawl4AIScraper:
    """Wrapper around crawl4ai for scraping web pages."""

    def __init__(self, max_concurrent: int = 5):
        self.max_concurrent = max_concurrent
        self.crawler: Optional[AsyncWebCrawler] = None
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._initialized = False

    async def initialize(self, ignore_https_errors: bool = False):
        """Initialize the crawler with browser configuration."""
        if self._initialized:
            return

        # Build proxy configuration
        proxy_config = None
        if PROXY_SERVER:
            proxy_config = {
                "server": PROXY_SERVER,
            }
            if PROXY_USERNAME and PROXY_PASSWORD:
                proxy_config["username"] = PROXY_USERNAME
                proxy_config["password"] = PROXY_PASSWORD

        # Browser configuration
        # Enable stealth mode to bypass basic bot detection (Duda, etc.)
        browser_config = BrowserConfig(
            headless=HEADLESS,
            verbose=False,
            proxy_config=proxy_config,
            ignore_https_errors=ignore_https_errors,
            # Enable stealth mode - uses playwright-stealth to modify browser fingerprints
            # Required for sites with bot detection like Duda-built websites
            enable_stealth=True,
            # Chrome args for Docker compatibility + anti-detection
            extra_args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu",
                # Anti-detection args
                "--disable-blink-features=AutomationControlled",
            ],
        )

        self.crawler = AsyncWebCrawler(config=browser_config)
        await self.crawler.start()
        self._initialized = True
        logger.info("Crawl4AI scraper initialized")

    async def close(self):
        """Close the crawler and release resources."""
        if self.crawler:
            await self.crawler.close()
            self.crawler = None
            self._initialized = False
            logger.info("Crawl4AI scraper closed")

    async def scrape(
        self,
        url: str,
        timeout: int = 30000,
        wait_after_load: int = 0,
        headers: Optional[dict[str, str]] = None,
        skip_tls_verification: bool = False,
        wait_for_selector: Optional[str] = None,
        load_all_content: bool = False,
        load_all_content_timeout: int = 30000,
    ) -> dict:
        """
        Scrape a URL and return HTML content and markdown.

        Args:
            url: The URL to scrape
            timeout: Request timeout in milliseconds
            wait_after_load: Additional wait time after page load in milliseconds
            headers: Custom HTTP headers
            skip_tls_verification: Whether to skip TLS certificate verification
            wait_for_selector: CSS selector to wait for before scraping
            load_all_content: If True, scroll through page and click "Load More" buttons
            load_all_content_timeout: Max time in ms for loading all content

        Returns:
            dict with html, markdown, status_code, and content_type
        """
        if not self._initialized:
            await self.initialize()

        async with self.semaphore:
            try:
                # JavaScript to click all "Load More" type buttons
                load_more_js = """
                (async () => {
                    const timeout = %d;
                    const startTime = Date.now();
                    const selectors = [
                        'button[class*="load-more"]',
                        'button[class*="loadMore"]',
                        'button[class*="LoadMore"]',
                        '[class*="load-more"] button',
                        '[class*="loadMore"] button',
                        'a[class*="load-more"]',
                        'a[class*="loadMore"]',
                        'button:has-text("Load More")',
                        'button:has-text("Show More")',
                        'button:has-text("See More")',
                        'button:has-text("View More")',
                        '[role="button"][class*="load"]',
                    ];

                    const findLoadMoreButtons = () => {
                        for (const selector of selectors) {
                            try {
                                const buttons = document.querySelectorAll(selector);
                                if (buttons.length > 0) return Array.from(buttons);
                            } catch (e) {}
                        }
                        // Fallback: find buttons by text content
                        const allButtons = document.querySelectorAll('button, [role="button"], a.btn');
                        return Array.from(allButtons).filter(btn => {
                            const text = btn.textContent?.toLowerCase() || '';
                            return text.includes('load more') || text.includes('show more') ||
                                   text.includes('see more') || text.includes('view more');
                        });
                    };

                    let iterations = 0;
                    const maxIterations = 50;

                    while (Date.now() - startTime < timeout && iterations < maxIterations) {
                        const buttons = findLoadMoreButtons();
                        if (buttons.length === 0) break;

                        for (const btn of buttons) {
                            if (btn.offsetParent !== null) {  // Check if visible
                                btn.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                await new Promise(r => setTimeout(r, 300));
                                btn.click();
                                await new Promise(r => setTimeout(r, 1000));
                            }
                        }
                        iterations++;
                        await new Promise(r => setTimeout(r, 500));
                    }
                })();
                """ % load_all_content_timeout if load_all_content else None

                # Build crawler run configuration
                crawler_config = CrawlerRunConfig(
                    # Cache settings
                    cache_mode=CacheMode.BYPASS,

                    # Wait settings
                    page_timeout=timeout + (load_all_content_timeout if load_all_content else 0),
                    delay_before_return_html=wait_after_load / 1000.0 if wait_after_load > 0 else 0,

                    # Selector wait
                    wait_for=wait_for_selector if wait_for_selector else None,

                    # Content settings
                    remove_overlay_elements=True,
                    process_iframes=True,

                    # Full page scanning for lazy-loaded content
                    scan_full_page=load_all_content,
                    scroll_delay=0.5 if load_all_content else 0.0,

                    # JavaScript to execute (click Load More buttons)
                    js_code=load_more_js,

                    # Markdown generation
                    markdown_generator=DefaultMarkdownGenerator(
                        options={
                            "ignore_links": False,
                            "ignore_images": BLOCK_MEDIA,
                        }
                    ),

                    # Media blocking
                    excluded_tags=["script", "style", "noscript"] + (
                        ["img", "video", "audio", "picture", "source", "svg"]
                        if BLOCK_MEDIA else []
                    ),
                )

                # Set custom headers if provided
                if headers:
                    crawler_config.headers = headers

                if load_all_content:
                    logger.info(f"Scraping URL with load_all_content: {url} (timeout: {load_all_content_timeout}ms)")
                else:
                    logger.info(f"Scraping URL: {url}")
                result = await self.crawler.arun(url=url, config=crawler_config)

                if not result.success:
                    logger.warning(f"Scrape failed for {url}: {result.error_message}")
                    return {
                        "html": "",
                        "markdown": "",
                        "status_code": result.status_code or 500,
                        "content_type": "text/html",
                        "error": result.error_message,
                    }

                logger.info(f"Successfully scraped {url}")

                # Get HTML and markdown from result
                html = result.html or ""
                markdown = result.markdown or ""

                logger.info(f"Content sizes for {url}: html={len(html)}, markdown={len(markdown)}")

                # Enhance markdown with embedded JSON content for SPA sites
                # result.html contains the full rendered page with embedded JSON data
                enhanced_markdown = extract_embedded_json_content(html, markdown, url)

                return {
                    "html": html,
                    "markdown": enhanced_markdown,
                    "status_code": result.status_code or 200,
                    "content_type": result.response_headers.get("content-type", "text/html")
                    if result.response_headers else "text/html",
                }

            except asyncio.TimeoutError:
                logger.error(f"Timeout scraping {url}")
                return {
                    "html": "",
                    "markdown": "",
                    "status_code": 408,
                    "content_type": "text/html",
                    "error": "Request timeout",
                }
            except Exception as e:
                logger.error(f"Error scraping {url}: {e}")
                return {
                    "html": "",
                    "markdown": "",
                    "status_code": 500,
                    "content_type": "text/html",
                    "error": str(e),
                }
