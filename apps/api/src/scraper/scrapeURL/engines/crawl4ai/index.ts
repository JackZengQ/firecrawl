import { z } from "zod";
import { EngineScrapeResult } from "..";
import { Meta } from "../..";
import { robustFetch } from "../../lib/fetch";

/**
 * Scrape a URL using the Crawl4AI microservice.
 * This is similar to the Playwright engine but uses the crawl4ai library
 * which provides built-in markdown conversion.
 */
export async function scrapeURLWithCrawl4AI(
  meta: Meta,
): Promise<EngineScrapeResult> {
  const response = await robustFetch({
    url: process.env.CRAWL4AI_MICROSERVICE_URL!,
    headers: {
      "Content-Type": "application/json",
    },
    body: {
      url: meta.rewrittenUrl ?? meta.url,
      wait_after_load: meta.options.waitFor ?? 0,
      timeout: meta.abort.scrapeTimeout(),
      headers: meta.options.headers,
      skip_tls_verification: meta.options.skipTlsVerification ?? false,
      extract_markdown: true,
      // Enable full page scanning with scroll for lazy-loaded content
      load_all_content: (meta.options as any).loadAllContent ?? false,
      load_all_content_timeout: (meta.options as any).loadAllContentTimeout ?? 30000,
    },
    method: "POST",
    logger: meta.logger.child("scrapeURLWithCrawl4AI/robustFetch"),
    schema: z.object({
      content: z.string(),
      markdown: z.string().optional().nullable(),
      pageStatusCode: z.number(),
      pageError: z.string().optional().nullable(),
      contentType: z.string().optional().nullable(),
    }),
    mock: meta.mock,
    abort: meta.abort.asSignal(),
  });

  return {
    url: meta.rewrittenUrl ?? meta.url,
    html: response.content,
    markdown: response.markdown ?? undefined,
    statusCode: response.pageStatusCode,
    error: response.pageError ?? undefined,
    contentType: response.contentType ?? undefined,

    proxyUsed: "basic",
  };
}

export function crawl4aiMaxReasonableTime(meta: Meta): number {
  return (meta.options.waitFor ?? 0) + 60000; // crawl4ai may be slower due to markdown generation
}
