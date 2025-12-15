import { logger } from "./logger";
import { config } from "../config";
import crypto from "crypto";
import { storage } from "./gcs-jobs";
import {
  isS3Configured,
  getMainBucket,
  s3SaveWithRetry,
  s3Get,
  s3Exists,
} from "./s3-storage";

const PDF_CACHE_PREFIX = "pdf-cache-v2/";

// Helper to determine which storage backend to use
function useS3(): boolean {
  return isS3Configured();
}

// Helper to get bucket name
function getBucketName(): string | undefined {
  return getMainBucket();
}

/**
 * Creates a SHA-256 hash of the PDF content to use as a cache key
 * Directly hashes the content without any conversion
 */
function createPdfCacheKey(pdfContent: string | Buffer): string {
  return crypto.createHash("sha256").update(pdfContent).digest("hex");
}

/**
 * Save RunPod markdown results to cache (S3 or GCS)
 */
export async function savePdfResultToCache(
  pdfContent: string,
  result: { markdown: string; html: string },
): Promise<string | null> {
  try {
    const bucketName = getBucketName();
    if (!bucketName) {
      return null;
    }

    const cacheKey = createPdfCacheKey(pdfContent);
    const key = `${PDF_CACHE_PREFIX}${cacheKey}.json`;
    const content = JSON.stringify(result);
    const metadata = {
      source: "runpod_pdf_conversion",
      cache_type: "pdf_markdown",
      created_at: new Date().toISOString(),
    };

    if (useS3()) {
      await s3SaveWithRetry(bucketName, key, content, "application/json", metadata);
      logger.info(`Saved PDF RunPod result to S3 cache`, { cacheKey });
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      for (let i = 0; i < 3; i++) {
        try {
          await blob.save(content, {
            contentType: "application/json",
            metadata,
          });

          logger.info(`Saved PDF RunPod result to GCS cache`, { cacheKey });
          break;
        } catch (error) {
          if (i === 2) {
            throw error;
          } else {
            logger.error(
              `Error saving PDF RunPod result to GCS cache, retrying`,
              {
                error,
                cacheKey,
                i,
              },
            );
          }
        }
      }
    }

    return cacheKey;
  } catch (error) {
    logger.error(`Error saving PDF RunPod result to cache`, { error });
    return null;
  }
}

/**
 * Get cached RunPod markdown results from cache (S3 or GCS)
 */
export async function getPdfResultFromCache(
  pdfContent: string,
): Promise<{ markdown: string; html: string } | null> {
  try {
    const bucketName = getBucketName();
    if (!bucketName) {
      return null;
    }

    const cacheKey = createPdfCacheKey(pdfContent);
    const key = `${PDF_CACHE_PREFIX}${cacheKey}.json`;

    if (useS3()) {
      const content = await s3Get(bucketName, key);
      if (!content) {
        logger.debug(`PDF RunPod result not found in S3 cache`, { cacheKey });
        return null;
      }

      const result = JSON.parse(content);
      logger.info(`Retrieved PDF RunPod result from S3 cache`, { cacheKey });
      return result;
    } else {
      const bucket = storage.bucket(bucketName);
      const blob = bucket.file(key);

      const [exists] = await blob.exists();
      if (!exists) {
        logger.debug(`PDF RunPod result not found in GCS cache`, { cacheKey });
        return null;
      }

      const [content] = await blob.download();
      const result = JSON.parse(content.toString());

      logger.info(`Retrieved PDF RunPod result from GCS cache`, { cacheKey });
      return result;
    }
  } catch (error) {
    logger.error(`Error retrieving PDF RunPod result from cache`, { error });
    return null;
  }
}
