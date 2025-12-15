import { ApiError, Storage } from "@google-cloud/storage";
import { logger } from "./logger";
import { Document } from "../controllers/v1/types";
import { withSpan, setSpanAttributes } from "./otel-tracer";
import type {
  LoggedDeepResearch,
  LoggedExtract,
  LoggedLlmsTxt,
  LoggedMap,
  LoggedScrape,
  LoggedSearch,
} from "../services/logging/log_job";
import { config } from "../config";
import {
  isS3Configured,
  getMainBucket,
  s3SaveWithRetry,
  s3Get,
  s3Delete,
} from "./s3-storage";

// GCS client (legacy - used as fallback when S3 is not configured)
const credentials = config.GCS_CREDENTIALS
  ? JSON.parse(atob(config.GCS_CREDENTIALS))
  : undefined;
export const storage = new Storage({ credentials });

// Helper to determine which storage backend to use
function useS3(): boolean {
  return isS3Configured();
}

// Helper to get bucket name
function getBucketName(): string | undefined {
  return getMainBucket();
}

export async function saveScrapeToGCS(scrape: LoggedScrape): Promise<void> {
  return await withSpan("firecrawl-gcs-save-job", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_job",
      "job.id": scrape.id,
      "job.team_id": scrape.team_id,
      "job.mode": "scrape",
      "job.success": scrape.is_successful,
      "job.num_docs": 1,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${scrape.id}.json`;
    const content = JSON.stringify([scrape.doc]);
    const metadata = {
      job_id: scrape.id ?? "",
      success: String(scrape.is_successful),
      message: scrape.zeroDataRetention ? "" : (scrape.error ?? ""),
      num_docs: "1",
      time_taken: String(scrape.time_taken),
      team_id:
        scrape.team_id === "preview" || scrape.team_id?.startsWith("preview_")
          ? ""
          : (scrape.team_id ?? ""),
      mode: "scrape",
      url: scrape.zeroDataRetention
        ? "<redacted due to zero data retention>"
        : scrape.url,
      page_options: scrape.zeroDataRetention
        ? ""
        : JSON.stringify(scrape.options),
      request_id: scrape.request_id ?? "",
    };

    if (useS3()) {
      // Use S3-compatible storage
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      // Fall back to GCS
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      // Save job docs with retry
      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, {
            contentType: "application/json",
          });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving job to GCS, retrying`, {
              error,
              scrapeId: scrape.id,
              jobId: scrape.id,
              i,
              zeroDataRetention: scrape.zeroDataRetention,
            });
          }
        }
      }

      // Save job metadata with retry
      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving scrape metadata to GCS, retrying`, {
              error,
              scrapeId: scrape.id,
              jobId: scrape.id,
              i,
              zeroDataRetention: scrape.zeroDataRetention,
            });
          }
        }
      }

      setSpanAttributes(span, { "storage.backend": "gcs", "gcs.save_successful": true });
    }
  }).catch(error => {
    logger.error(`Error saving scrape to storage`, {
      error,
      scrapeId: scrape.id,
      jobId: scrape.id,
      zeroDataRetention: scrape.zeroDataRetention,
    });
    // Don't throw - storage failures should not fail the scrape
    // This allows scraping to work even without S3/GCS configured
  });
}

export async function saveSearchToGCS(search: LoggedSearch): Promise<void> {
  return await withSpan("firecrawl-gcs-save-search", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_search",
      "search.id": search.id,
      "search.team_id": search.team_id,
      request_id: search.request_id,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${search.id}.json`;
    const content = JSON.stringify(search.results);
    const metadata = {
      mode: "search",
      job_id: search.id,
      num_docs: String(search.num_results),
      time_taken: String(search.time_taken),
      team_id:
        search.team_id === "preview" || search.team_id?.startsWith("preview_")
          ? ""
          : (search.team_id ?? ""),
      query: search.zeroDataRetention
        ? "<redacted due to zero data retention>"
        : search.query,
      options: search.zeroDataRetention ? "" : JSON.stringify(search.options),
      credits_cost: String(search.credits_cost),
      success: String(search.is_successful),
      error: search.zeroDataRetention ? "" : (search.error ?? ""),
      num_results: String(search.num_results),
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, { contentType: "application/json" });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving search to GCS, retrying`, {
              error,
              searchId: search.id,
              i,
            });
          }
        }
      }

      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving search metadata to GCS, retrying`, {
              error,
              searchId: search.id,
              i,
            });
          }
        }
      }
      setSpanAttributes(span, { "storage.backend": "gcs" });
    }
  });
}

export async function saveExtractToGCS(extract: LoggedExtract): Promise<void> {
  return await withSpan("firecrawl-gcs-save-extract", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_extract",
      "extract.id": extract.id,
      "extract.team_id": extract.team_id,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${extract.id}.json`;
    const content = JSON.stringify(extract.result);
    const metadata = {
      mode: "extract",
      job_id: extract.id,
      num_docs: "1",
      team_id:
        extract.team_id === "preview" || extract.team_id?.startsWith("preview_")
          ? ""
          : (extract.team_id ?? ""),
      options: JSON.stringify(extract.options),
      credits_cost: String(extract.credits_cost),
      success: String(extract.is_successful),
      error: extract.error ?? "",
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, { contentType: "application/json" });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving extract to GCS, retrying`, {
              error,
              extractId: extract.id,
              i,
            });
          }
        }
      }

      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving extract metadata to GCS, retrying`, {
              error,
              extractId: extract.id,
              i,
            });
          }
        }
      }
      setSpanAttributes(span, { "storage.backend": "gcs" });
    }

    setSpanAttributes(span, { "gcs.save_successful": true });
    return;
  });
}

export async function saveMapToGCS(map: LoggedMap): Promise<void> {
  return await withSpan("firecrawl-gcs-save-map", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_map",
      "map.id": map.id,
      "map.team_id": map.team_id,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${map.id}.json`;
    const content = JSON.stringify(map.results);
    const metadata = {
      mode: "map",
      job_id: map.id,
      num_results: String(map.results.length),
      team_id:
        map.team_id === "preview" || map.team_id?.startsWith("preview_")
          ? ""
          : (map.team_id ?? ""),
      options: JSON.stringify(map.options),
      credits_cost: String(map.credits_cost),
      success: "true",
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, { contentType: "application/json" });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving map to GCS, retrying`, {
              error,
              mapId: map.id,
              i,
            });
          }
        }
      }

      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving map metadata to GCS, retrying`, {
              error,
              mapId: map.id,
              i,
            });
          }
        }
      }
      setSpanAttributes(span, { "storage.backend": "gcs" });
    }

    setSpanAttributes(span, { "gcs.save_successful": true });
    return;
  });
}

export async function saveDeepResearchToGCS(
  deepResearch: LoggedDeepResearch,
): Promise<void> {
  return await withSpan("firecrawl-gcs-save-deep-research", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_deep_research",
      "deep_research.id": deepResearch.id,
      "deep_research.team_id": deepResearch.team_id,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${deepResearch.id}.json`;
    const content = JSON.stringify(deepResearch.result);
    const metadata = {
      mode: "deep_research",
      job_id: deepResearch.id,
      team_id:
        deepResearch.team_id === "preview" ||
        deepResearch.team_id?.startsWith("preview_")
          ? ""
          : (deepResearch.team_id ?? ""),
      options: JSON.stringify(deepResearch.options),
      credits_cost: String(deepResearch.credits_cost),
      success: "true",
      time_taken: String(deepResearch.time_taken),
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, { contentType: "application/json" });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving deep research to GCS, retrying`, {
              error,
              deepResearchId: deepResearch.id,
              i,
            });
          }
        }
      }

      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving deep research metadata to GCS, retrying`, {
              error,
              deepResearchId: deepResearch.id,
              i,
            });
          }
        }
      }
      setSpanAttributes(span, { "storage.backend": "gcs" });
    }

    setSpanAttributes(span, { "gcs.save_successful": true });
    return;
  });
}

export async function saveLlmsTxtToGCS(llmsTxt: LoggedLlmsTxt): Promise<void> {
  return await withSpan("firecrawl-gcs-save-llms-txt", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "save_llms_txt",
      "llms_txt.id": llmsTxt.id,
      "llms_txt.team_id": llmsTxt.team_id,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${llmsTxt.id}.json`;
    const content = JSON.stringify(llmsTxt.result);
    const metadata = {
      mode: "llms_txt",
      job_id: llmsTxt.id,
      team_id:
        llmsTxt.team_id === "preview" || llmsTxt.team_id?.startsWith("preview_")
          ? ""
          : (llmsTxt.team_id ?? ""),
      options: JSON.stringify(llmsTxt.options),
      credits_cost: String(llmsTxt.credits_cost),
      success: "true",
      num_urls: String(llmsTxt.num_urls),
      cost_tracking: JSON.stringify(llmsTxt.cost_tracking),
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      setSpanAttributes(span, { "storage.backend": "s3", "gcs.save_successful": true });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, { contentType: "application/json" });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving llms txt to GCS, retrying`, {
              error,
              llmsTxtId: llmsTxt.id,
              i,
            });
          }
        }
      }

      for (let i = 0; i < 3; i++) {
        try {
          await blob.setMetadata({ metadata });
          setSpanAttributes(span, { "gcs.save_successful": true });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(`Error saving llms txt metadata to GCS, retrying`, {
              error,
              llmsTxtId: llmsTxt.id,
              i,
            });
          }
        }
      }
      setSpanAttributes(span, { "storage.backend": "gcs" });
    }

    setSpanAttributes(span, { "gcs.save_successful": true });
    return;
  });
}

export async function getJobFromGCS(jobId: string): Promise<Document[] | null> {
  return await withSpan("firecrawl-gcs-get-job", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "get_job",
      "job.id": jobId,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return null;
    }

    const key = `${jobId}.json`;

    if (useS3()) {
      try {
        const content = await s3Get(bucketName, key);
        if (!content) {
          setSpanAttributes(span, { "gcs.job_found": false });
          return null;
        }
        const result = JSON.parse(content);
        setSpanAttributes(span, { "storage.backend": "s3", "gcs.job_found": true });
        return result;
      } catch (error) {
        logger.error(`Error getting job from S3`, {
          error,
          jobId,
          scrapeId: jobId,
        });
        // Return null instead of throwing - treat storage errors as "not found"
        setSpanAttributes(span, { "gcs.job_found": false, "gcs.error": true });
        return null;
      }
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      try {
        const [content] = await blob.download();
        const result = JSON.parse(content.toString());
        setSpanAttributes(span, { "storage.backend": "gcs", "gcs.job_found": true });
        return result;
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.code === 404 &&
          error.message.includes("No such object:")
        ) {
          setSpanAttributes(span, { "gcs.job_found": false });
          return null;
        }

        logger.error(`Error getting job from GCS`, {
          error,
          jobId,
          scrapeId: jobId,
        });
        throw error;
      }
    }
  });
}

export async function removeJobFromGCS(jobId: string): Promise<void> {
  return await withSpan("firecrawl-gcs-remove-job", async span => {
    setSpanAttributes(span, {
      "gcs.operation": "remove_job",
      "job.id": jobId,
    });

    const bucketName = getBucketName();
    if (!bucketName) {
      setSpanAttributes(span, { "gcs.bucket_configured": false });
      return;
    }

    const key = `${jobId}.json`;

    if (useS3()) {
      try {
        await s3Delete(bucketName, key);
        setSpanAttributes(span, { "storage.backend": "s3", "gcs.delete_successful": true });
      } catch (error) {
        logger.error(`Error removing job from S3`, {
          error,
          jobId,
          scrapeId: jobId,
        });
        throw error;
      }
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      try {
        await blob.delete({
          ignoreNotFound: true,
        });
        setSpanAttributes(span, { "storage.backend": "gcs", "gcs.delete_successful": true });
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.code === 404 &&
          error.message.includes("No such object:")
        ) {
          setSpanAttributes(span, { "gcs.job_not_found": true });
          return;
        }

        logger.error(`Error removing job from GCS`, {
          error,
          jobId,
          scrapeId: jobId,
        });
        throw error;
      }
    }
  });
}

// TODO: fix the any type (we have multiple Document types in the codebase)
export async function getDocFromGCS(url: string): Promise<any | null> {
  // This function uses a different bucket (GCS_FIRE_ENGINE_BUCKET_NAME)
  // For now, keep it GCS-only as it's specific to fire-engine integration
  try {
    if (!config.GCS_FIRE_ENGINE_BUCKET_NAME) {
      return null;
    }

    const bucket = storage.bucket(config.GCS_FIRE_ENGINE_BUCKET_NAME);
    const blob = bucket.file(`${url}`);
    const [exists] = await blob.exists();
    if (!exists) {
      return null;
    }
    const [blobContent] = await blob.download();
    const parsed = JSON.parse(blobContent.toString());
    return parsed;
  } catch (error) {
    logger.error(`Error getting f-engine document from GCS`, {
      error,
      url,
    });
    return null;
  }
}
