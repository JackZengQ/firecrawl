// S3-Compatible Storage Module
// Works with AWS S3, Supabase Storage, MinIO, and other S3-compatible services

import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  DeleteObjectCommand,
  HeadObjectCommand,
  CreateBucketCommand,
  HeadBucketCommand,
} from "@aws-sdk/client-s3";
import { config } from "../config";
import { logger } from "./logger";

// Lazy-initialized S3 client
let s3Client: S3Client | null = null;
let bucketsInitialized = false;

async function ensureBucketExists(client: S3Client, bucket: string): Promise<void> {
  try {
    await client.send(new HeadBucketCommand({ Bucket: bucket }));
  } catch (error: any) {
    if (error.name === "NotFound" || error.$metadata?.httpStatusCode === 404) {
      logger.info(`Creating S3 bucket: ${bucket}`);
      await client.send(new CreateBucketCommand({ Bucket: bucket }));
    } else {
      throw error;
    }
  }
}

export async function initS3Buckets(): Promise<void> {
  if (bucketsInitialized || !isS3Configured()) {
    return;
  }

  const client = getS3Client();
  if (!client) {
    return;
  }

  const buckets = [
    config.S3_BUCKET_NAME,
    config.S3_INDEX_BUCKET_NAME,
    config.S3_MEDIA_BUCKET_NAME,
  ].filter(Boolean) as string[];

  for (const bucket of buckets) {
    await ensureBucketExists(client, bucket);
  }

  bucketsInitialized = true;
  logger.info("S3 buckets initialized");
}

export function getS3Client(): S3Client | null {
  if (s3Client) {
    return s3Client;
  }

  if (!isS3Configured()) {
    return null;
  }

  s3Client = new S3Client({
    endpoint: config.S3_ENDPOINT,
    region: config.S3_REGION,
    credentials: {
      accessKeyId: config.S3_ACCESS_KEY!,
      secretAccessKey: config.S3_SECRET_KEY!,
    },
    forcePathStyle: config.S3_FORCE_PATH_STYLE,
  });

  return s3Client;
}

// Check if S3 storage is configured
export function isS3Configured(): boolean {
  return !!(
    config.S3_ENDPOINT &&
    config.S3_ACCESS_KEY &&
    config.S3_SECRET_KEY &&
    config.S3_BUCKET_NAME
  );
}

// Check if any storage (S3 or GCS) is configured for main bucket
export function isStorageConfigured(): boolean {
  return !!(config.S3_BUCKET_NAME || config.GCS_BUCKET_NAME);
}

// Get the appropriate bucket name
export function getMainBucket(): string | undefined {
  return config.S3_BUCKET_NAME || config.GCS_BUCKET_NAME;
}

export function getIndexBucket(): string | undefined {
  return config.S3_INDEX_BUCKET_NAME || config.GCS_INDEX_BUCKET_NAME;
}

export function getMediaBucket(): string | undefined {
  return config.S3_MEDIA_BUCKET_NAME || config.GCS_MEDIA_BUCKET_NAME;
}

// S3 Storage Operations
export async function s3Save(
  bucket: string,
  key: string,
  data: string | Buffer,
  contentType: string = "application/json",
  metadata?: Record<string, string>,
): Promise<void> {
  const client = getS3Client();
  if (!client) {
    throw new Error("S3 client is not configured");
  }

  const body = typeof data === "string" ? Buffer.from(data, "utf-8") : data;

  const command = new PutObjectCommand({
    Bucket: bucket,
    Key: key,
    Body: body,
    ContentType: contentType,
    Metadata: metadata,
  });

  await client.send(command);
}

export async function s3Get(
  bucket: string,
  key: string,
): Promise<string | null> {
  const client = getS3Client();
  if (!client) {
    throw new Error("S3 client is not configured");
  }

  try {
    const command = new GetObjectCommand({
      Bucket: bucket,
      Key: key,
    });

    const response = await client.send(command);

    if (!response.Body) {
      return null;
    }

    // Convert stream to string
    const chunks: Uint8Array[] = [];
    for await (const chunk of response.Body as AsyncIterable<Uint8Array>) {
      chunks.push(chunk);
    }
    return Buffer.concat(chunks).toString("utf-8");
  } catch (error: any) {
    if (error.name === "NoSuchKey" || error.$metadata?.httpStatusCode === 404) {
      return null;
    }
    throw error;
  }
}

export async function s3Delete(bucket: string, key: string): Promise<void> {
  const client = getS3Client();
  if (!client) {
    throw new Error("S3 client is not configured");
  }

  const command = new DeleteObjectCommand({
    Bucket: bucket,
    Key: key,
  });

  await client.send(command);
}

export async function s3Exists(bucket: string, key: string): Promise<boolean> {
  const client = getS3Client();
  if (!client) {
    throw new Error("S3 client is not configured");
  }

  try {
    const command = new HeadObjectCommand({
      Bucket: bucket,
      Key: key,
    });

    await client.send(command);
    return true;
  } catch (error: any) {
    if (error.name === "NotFound" || error.$metadata?.httpStatusCode === 404) {
      return false;
    }
    throw error;
  }
}

export async function s3SaveWithRetry(
  bucket: string,
  key: string,
  data: string | Buffer,
  contentType: string = "application/json",
  metadata?: Record<string, string>,
  maxRetries: number = 3,
): Promise<void> {
  let lastError: Error | null = null;

  for (let i = 0; i < maxRetries; i++) {
    try {
      await s3Save(bucket, key, data, contentType, metadata);
      return;
    } catch (error) {
      lastError = error as Error;
      logger.error(`Error saving to S3, attempt ${i + 1}/${maxRetries}`, {
        error,
        bucket,
        key,
      });

      if (i < maxRetries - 1) {
        // Wait before retry with exponential backoff
        await new Promise(resolve => setTimeout(resolve, Math.pow(2, i) * 100));
      }
    }
  }

  throw lastError;
}
