"""
Crawl4AI Scraper Wrapper
Provides a clean interface to crawl4ai for the Firecrawl microservice.
"""

import asyncio
import logging
import os
from typing import Optional

from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

logger = logging.getLogger("crawl4ai-service.scraper")

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
        browser_config = BrowserConfig(
            headless=HEADLESS,
            verbose=False,
            proxy_config=proxy_config,
            ignore_https_errors=ignore_https_errors,
            # Chrome args for Docker compatibility
            extra_args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--no-first-run",
                "--no-zygote",
                "--disable-gpu",
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

        Returns:
            dict with html, markdown, status_code, and content_type
        """
        if not self._initialized:
            await self.initialize()

        async with self.semaphore:
            try:
                # Build crawler run configuration
                crawler_config = CrawlerRunConfig(
                    # Cache settings
                    cache_mode=CacheMode.BYPASS,

                    # Wait settings
                    page_timeout=timeout,
                    delay_before_return_html=wait_after_load / 1000.0 if wait_after_load > 0 else 0,

                    # Selector wait
                    wait_for=wait_for_selector if wait_for_selector else None,

                    # Content settings
                    remove_overlay_elements=True,
                    process_iframes=True,

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

                return {
                    "html": result.html or "",
                    "markdown": result.markdown or "",
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
