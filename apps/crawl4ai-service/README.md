# Crawl4AI Service for Firecrawl

A standalone microservice that uses [Crawl4AI](https://github.com/unclecode/crawl4ai) as the scraping engine for Firecrawl. It runs as a Docker container, picks up scraping jobs from Firecrawl's NuQ queue, and returns markdown results.

## Table of Contents

- [Features](#features)
- [Setup](#setup)
- [Configuration](#configuration)
- [Configuring Firecrawl to Use Crawl4AI](#configuring-firecrawl-to-use-crawl4ai)
- [API Endpoints](#api-endpoints)
- [How It Works](#how-it-works)
- [LLM Extraction (Optional)](#llm-extraction-optional)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Features

- **Built-in Markdown Conversion**: Crawl4AI automatically converts HTML to clean markdown
- **Queue-based Processing**: Consumes jobs from NuQ PostgreSQL queue
- **Direct HTTP API**: Can also be called directly via `/scrape` endpoint
- **Concurrent Scraping**: Configurable worker pool for parallel processing
- **Distributed Locking**: Prevents duplicate job processing across multiple instances
- **Health Monitoring**: `/health` and `/metrics` endpoints for observability

## Setup

### Prerequisites

- Docker and Docker Compose (for containerized setup)
- Python 3.11+ (for local development)
- PostgreSQL with NuQ schema (provided by `nuq-postgres` service)
- Firecrawl API (to use the engine integration)

### Option 1: Full Stack with Docker Compose (Recommended)

This starts all Firecrawl services including crawl4ai-service:

```bash
# Clone the repository (if not already done)
git clone https://github.com/mendableai/firecrawl.git
cd firecrawl

# Start all services
docker-compose up -d

# Verify crawl4ai-service is running
docker-compose ps crawl4ai-service
curl http://localhost:3001/health  # Note: may need to expose port first
```

The crawl4ai-service will automatically:
- Connect to the NuQ PostgreSQL database
- Start polling for jobs with `engine=crawl4ai`
- Be available at `http://crawl4ai-service:3001` within the Docker network

### Option 2: Standalone Docker Container

If you want to run crawl4ai-service separately:

```bash
cd apps/crawl4ai-service

# Build the image
docker build -t crawl4ai-service .

# Run with connection to existing NuQ database
docker run -d \
  --name crawl4ai-service \
  -p 3001:3001 \
  -e NUQ_DATABASE_URL=postgres://postgres:postgres@host.docker.internal:5435/postgres \
  -e MAX_CONCURRENT_SCRAPES=5 \
  -e LOGGING_LEVEL=INFO \
  crawl4ai-service

# Check logs
docker logs -f crawl4ai-service
```

### Option 3: Local Development Setup

For development without Docker:

```bash
cd apps/crawl4ai-service

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers (required by crawl4ai)
playwright install chromium
playwright install-deps chromium  # Install system dependencies

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings (especially NUQ_DATABASE_URL)

# Start the NuQ PostgreSQL database (if not running)
docker-compose up -d nuq-postgres

# Run the service
python src/main.py
```

### Option 4: Add to Existing Firecrawl Installation

If you have Firecrawl already running and want to add crawl4ai:

1. **Build the crawl4ai-service image:**
   ```bash
   cd apps/crawl4ai-service
   docker build -t crawl4ai-service .
   ```

2. **Add to your docker-compose.yaml:**
   ```yaml
   services:
     crawl4ai-service:
       image: crawl4ai-service
       environment:
         PORT: 3001
         NUQ_DATABASE_URL: postgres://postgres:postgres@nuq-postgres:5432/postgres
         MAX_CONCURRENT_SCRAPES: 5
         CRAWL4AI_ENGINE_MARKER: crawl4ai
         LOGGING_LEVEL: INFO
       networks:
         - backend
       depends_on:
         - nuq-postgres
   ```

3. **Add the environment variable to your API service:**
   ```yaml
   api:
     environment:
       CRAWL4AI_MICROSERVICE_URL: http://crawl4ai-service:3001/scrape
   ```

4. **Restart services:**
   ```bash
   docker-compose up -d
   ```

### Verifying the Setup

After setup, verify everything is working:

```bash
# 1. Check service health
curl http://localhost:3001/health
# Expected: {"status":"healthy","active_scrapes":0,"max_concurrent_scrapes":5,"database_connected":true}

# 2. Test direct scraping
curl -X POST http://localhost:3001/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
# Expected: {"content":"<html>...","markdown":"# Example Domain\n...","pageStatusCode":200,...}

# 3. Test via Firecrawl API (with engine forcing)
curl -X POST http://localhost:3002/v1/scrape \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"url":"https://example.com"}'
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `3001` | HTTP server port |
| `NUQ_DATABASE_URL` | `postgres://postgres:postgres@nuq-postgres:5432/postgres` | PostgreSQL connection string |
| `MAX_CONCURRENT_SCRAPES` | `5` | Number of concurrent scraping workers |
| `CRAWL4AI_ENGINE_MARKER` | `crawl4ai` | Engine marker to filter jobs from queue |
| `LOCK_RENEW_INTERVAL` | `10` | Seconds between lock renewals |
| `JOB_POLL_INTERVAL` | `0.5` | Seconds between queue polls (when idle) |
| `MAX_BACKOFF_INTERVAL` | `10.0` | Maximum backoff interval in seconds |
| `HEADLESS` | `true` | Run browser in headless mode |
| `BLOCK_MEDIA` | `false` | Block images/videos/audio |
| `LOGGING_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `PROXY_SERVER` | - | Proxy server URL |
| `PROXY_USERNAME` | - | Proxy authentication username |
| `PROXY_PASSWORD` | - | Proxy authentication password |

## Configuring Firecrawl to Use Crawl4AI

### Option 1: Automatic (Docker Compose)

When using `docker-compose up`, the API is automatically configured with:
```
CRAWL4AI_MICROSERVICE_URL=http://crawl4ai-service:3001/scrape
```

This adds `crawl4ai` to the available engines list.

### Option 2: Manual Configuration

Set the environment variable for the Firecrawl API:
```bash
export CRAWL4AI_MICROSERVICE_URL=http://localhost:3001/scrape
```

### Option 3: Force Crawl4AI for Specific Domains

Use the `FORCED_ENGINE_DOMAINS` environment variable to force crawl4ai for specific domains:

```bash
# Force crawl4ai for specific domains
export FORCED_ENGINE_DOMAINS='{"example.com":"crawl4ai","blog.mysite.org":"crawl4ai"}'

# Force crawl4ai with fallback to playwright
export FORCED_ENGINE_DOMAINS='{"example.com":["crawl4ai","playwright"]}'

# Force crawl4ai for all subdomains of a domain
export FORCED_ENGINE_DOMAINS='{"*.example.com":"crawl4ai"}'
```

## API Endpoints

### POST /scrape

Direct scraping endpoint (similar to playwright-service).

**Request:**
```json
{
  "url": "https://example.com",
  "wait_after_load": 0,
  "timeout": 30000,
  "headers": {"User-Agent": "Custom Agent"},
  "skip_tls_verification": false,
  "wait_for_selector": "#content",
  "extract_markdown": true
}
```

**Response:**
```json
{
  "content": "<html>...</html>",
  "markdown": "# Page Title\n\nContent...",
  "pageStatusCode": 200,
  "contentType": "text/html",
  "pageError": null
}
```

### GET /health

Health check endpoint.

**Response:**
```json
{
  "status": "healthy",
  "active_scrapes": 2,
  "max_concurrent_scrapes": 5,
  "database_connected": true
}
```

### GET /metrics

Basic metrics endpoint.

**Response:**
```json
{
  "active_jobs": 2,
  "max_concurrent_scrapes": 5,
  "workers": 5,
  "is_shutting_down": false
}
```

## How It Works

### Queue Mode (Default)

1. The service starts multiple worker coroutines (configured by `MAX_CONCURRENT_SCRAPES`)
2. Each worker polls the NuQ `queue_scrape` table for jobs where:
   - `status = 'queued'`
   - `data->>'engine' = 'crawl4ai'` OR `data->'scrapeOptions'->>'engine' = 'crawl4ai'`
3. When a job is found:
   - The worker atomically claims it with a distributed lock
   - A background task periodically renews the lock
   - The URL is scraped using Crawl4AI
   - Results (HTML + markdown) are stored in `returnvalue`
   - Job status is updated to `completed` or `failed`
   - A PostgreSQL NOTIFY is sent for listeners

### Direct Mode

Call the `/scrape` endpoint directly for synchronous scraping without using the queue.

## Engine Selection in Firecrawl

When `CRAWL4AI_MICROSERVICE_URL` is set, `crawl4ai` becomes available as an engine with these characteristics:

| Property | Value |
|----------|-------|
| Quality Score | 15 (between playwright:20 and fetch:5) |
| Supports waitFor | Yes |
| Supports skipTlsVerification | Yes |
| Supports actions | No |
| Supports screenshots | No |
| Built-in Markdown | Yes |

The engine selection algorithm will automatically choose crawl4ai when:
- Higher-quality engines (fire-engine, playwright) are unavailable or fail
- The domain is configured in `FORCED_ENGINE_DOMAINS`

## LLM Extraction (Optional)

By default, crawl4ai-service uses **rule-based markdown conversion** which does NOT require any LLM. However, Crawl4AI supports optional LLM-powered extraction for more advanced use cases.

### Default Mode (No LLM Required)

The current implementation uses:
- `DefaultMarkdownGenerator` - Rule-based HTML to Markdown conversion
- `AsyncWebCrawler` - Playwright-based browser automation

This is fast, free, and works without any API keys.

### Enabling LLM Extraction

To enable LLM-powered structured data extraction, you need to:

#### 1. Add LLM Provider Environment Variables

Add these to your `docker-compose.yaml` or `.env`:

```yaml
# For OpenAI
crawl4ai-service:
  environment:
    OPENAI_API_KEY: sk-your-api-key
    LLM_PROVIDER: openai/gpt-4o-mini

# For Ollama (local LLM)
crawl4ai-service:
  environment:
    OLLAMA_BASE_URL: http://ollama:11434
    LLM_PROVIDER: ollama/llama3.2

# For Anthropic Claude
crawl4ai-service:
  environment:
    ANTHROPIC_API_KEY: sk-ant-your-api-key
    LLM_PROVIDER: anthropic/claude-3-haiku
```

#### 2. Modify the Scraper Code

Update `src/scraper.py` to use `LLMExtractionStrategy`:

```python
from crawl4ai.extraction_strategy import LLMExtractionStrategy
from crawl4ai import LLMConfig

# Define your extraction schema (Pydantic model)
from pydantic import BaseModel
from typing import List

class Article(BaseModel):
    title: str
    author: str
    content: str
    tags: List[str]

# Create LLM extraction strategy
llm_strategy = LLMExtractionStrategy(
    llm_config=LLMConfig(
        provider=os.getenv("LLM_PROVIDER", "openai/gpt-4o-mini"),
        api_token=os.getenv("OPENAI_API_KEY"),
    ),
    schema=Article.model_json_schema(),
    extraction_type="schema",
    instruction="Extract the article information from this page.",
    chunk_token_threshold=4000,  # Split large content into chunks
)

# Use in crawler config
crawler_config = CrawlerRunConfig(
    extraction_strategy=llm_strategy,
    # ... other options
)
```

#### 3. Available LLM Providers

Crawl4AI uses [LiteLLM](https://docs.litellm.ai/docs/providers) under the hood, supporting:

| Provider | Model Format | Required Env Var |
|----------|--------------|------------------|
| OpenAI | `openai/gpt-4o-mini` | `OPENAI_API_KEY` |
| Anthropic | `anthropic/claude-3-haiku` | `ANTHROPIC_API_KEY` |
| Ollama | `ollama/llama3.2` | `OLLAMA_BASE_URL` |
| Azure OpenAI | `azure/deployment-name` | `AZURE_API_KEY`, `AZURE_API_BASE` |
| Google | `gemini/gemini-pro` | `GOOGLE_API_KEY` |
| Groq | `groq/llama-3.1-70b` | `GROQ_API_KEY` |

#### 4. LLM-Free Alternatives

For structured extraction without LLM costs, consider:

```python
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy

# CSS-based extraction (no LLM needed)
css_strategy = JsonCssExtractionStrategy(
    schema={
        "name": "articles",
        "baseSelector": "article.post",
        "fields": [
            {"name": "title", "selector": "h1", "type": "text"},
            {"name": "author", "selector": ".author", "type": "text"},
            {"name": "content", "selector": ".content", "type": "text"},
        ]
    }
)
```

### When to Use LLM Extraction

| Use Case | Recommended Approach |
|----------|---------------------|
| Basic scraping + markdown | Default (no LLM) |
| Structured data from consistent HTML | `JsonCssExtractionStrategy` |
| Complex/variable page structures | `LLMExtractionStrategy` |
| Semantic understanding required | `LLMExtractionStrategy` |

### Cost Considerations

- **Default mode**: Free (rule-based)
- **CSS/XPath extraction**: Free (rule-based)
- **LLM extraction**: Costs per API call (varies by provider)

For high-volume scraping, consider using Ollama for local LLM inference to avoid API costs.

## Development

### Local Development

```bash
cd apps/crawl4ai-service

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browsers
playwright install chromium

# Run the service
python src/main.py
```

### Running Tests

```bash
# TODO: Add test suite
pytest tests/
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Firecrawl API                            │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │   /scrape   │───▶│   Engine    │───▶│ crawl4ai engine     │ │
│  │  endpoint   │    │  Selection  │    │ (HTTP call)         │ │
│  └─────────────┘    └─────────────┘    └──────────┬──────────┘ │
│                                                    │            │
│  ┌─────────────┐                                   │            │
│  │   /crawl    │───▶ NuQ Queue ─────────────────┐  │            │
│  │  endpoint   │    (PostgreSQL)                │  │            │
│  └─────────────┘                                │  │            │
└─────────────────────────────────────────────────│──│────────────┘
                                                  │  │
                                                  ▼  ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Crawl4AI Service                            │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │  /scrape    │◀───│   FastAPI   │◀───│  HTTP from API      │ │
│  │  endpoint   │    │   Server    │    │                     │ │
│  └─────────────┘    └─────────────┘    └─────────────────────┘ │
│                                                                 │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │   Worker    │◀───│  NuQ Queue  │◀───│  PostgreSQL         │ │
│  │   Pool      │    │  Consumer   │    │  (queue_scrape)     │ │
│  └──────┬──────┘    └─────────────┘    └─────────────────────┘ │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    Crawl4AI Library                         ││
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐ ││
│  │  │  Playwright │  │   HTML to   │  │   Content           │ ││
│  │  │  Browser    │  │  Markdown   │  │   Extraction        │ ││
│  │  └─────────────┘  └─────────────┘  └─────────────────────┘ ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
```

## Troubleshooting

### Service not picking up jobs

1. Check that jobs have `engine: "crawl4ai"` in their data:
   ```sql
   SELECT * FROM nuq.queue_scrape
   WHERE data->>'engine' = 'crawl4ai'
      OR data->'scrapeOptions'->>'engine' = 'crawl4ai';
   ```

2. Verify database connectivity:
   ```bash
   curl http://localhost:3001/health
   ```

3. Check logs for errors:
   ```bash
   docker-compose logs crawl4ai-service
   ```

### Browser crashes

If you see browser-related errors, ensure the container has enough resources:
```yaml
# In docker-compose.yaml
crawl4ai-service:
  deploy:
    resources:
      limits:
        memory: 2G
```

### Slow scraping

- Increase `MAX_CONCURRENT_SCRAPES` for more parallelism
- Enable `BLOCK_MEDIA=true` to skip images/videos
- Use a proxy to avoid rate limiting

## License

Same as Firecrawl - see the main repository for license details.
