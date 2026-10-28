/**
 * Customer Key Management for ComputeCoin Private Jobs (Proof-of-Privacy).
 *
 * Requirements (ADR-0005, WP-D):
 * 1. The customer's X25519 private key is generated and stored EXCLUSIVELY in the browser (IndexedDB).
 * 2. It is NEVER transmitted to the platform, server, API, or any third party.
 * 3. Before submitting their first private job, users MUST download their key backup.
 * 4. Clear warning that losing this key means permanent loss of private job results.
 */

import { generateX25519Keypair, x25519PublicFromPrivate } from "./ccpp";

export interface StoredCustomerKey {
  id: "default";
  privateKeyHex: string;
  publicKeyHex: string;
  createdAt: string;
  backedUp: boolean;
}

const DB_NAME = "computecoin_privacy";
const DB_VERSION = 1;
const STORE_NAME = "keys";
const KEY_ID = "default";

// In-memory fallback for environments without window.indexedDB (Node/SSR/Vitest)
let memoryStore: StoredCustomerKey | null = null;

function hasIndexedDB(): boolean {
  return typeof window !== "undefined" && typeof window.indexedDB !== "undefined";
}

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    if (!hasIndexedDB()) {
      return reject(new Error("IndexedDB is not available in this environment"));
    }
    const req = window.indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error || new Error("Failed to open IndexedDB"));
  });
}

/**
 * Retrieve the active customer key, or null if not yet generated.
 */
export async function getCustomerKey(): Promise<StoredCustomerKey | null> {
  if (!hasIndexedDB()) {
    return memoryStore;
  }
  try {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE_NAME, "readonly");
      const store = tx.objectStore(STORE_NAME);
      const req = store.get(KEY_ID);
      req.onsuccess = () => resolve(req.result || null);
      req.onerror = () => reject(req.error);
    });
  } catch {
    return memoryStore;
  }
}

/**
 * Save or overwrite the customer key in storage.
 */
async function saveCustomerKey(record: StoredCustomerKey): Promise<void> {
  if (!hasIndexedDB()) {
    memoryStore = record;
    return;
  }
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const store = tx.objectStore(STORE_NAME);
    const req = store.put(record);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}

/**
 * Generate a new X25519 customer keypair and save it to browser storage.
 * If a key already exists, returns the existing key unless overwrite is true.
 */
export async function getOrGenerateCustomerKey(overwrite = false): Promise<StoredCustomerKey> {
  if (!overwrite) {
    const existing = await getCustomerKey();
    if (existing) {
      return existing;
    }
  }

  const kp = generateX25519Keypair();
  const record: StoredCustomerKey = {
    id: KEY_ID,
    privateKeyHex: kp.privateKeyHex,
    publicKeyHex: kp.publicKeyHex,
    createdAt: new Date().toISOString(),
    backedUp: false,
  };

  await saveCustomerKey(record);
  return record;
}

/**
 * Mark the customer key as backed up after user downloads the key file.
 */
export async function markCustomerKeyBackedUp(): Promise<void> {
  const existing = await getCustomerKey();
  if (!existing) {
    throw new Error("No customer key found to mark as backed up");
  }
  existing.backedUp = true;
  await saveCustomerKey(existing);
}

/**
 * Export customer key backup as a formatted JSON string.
 */
export async function exportCustomerKeyBackupJson(): Promise<string> {
  const key = await getCustomerKey();
  if (!key) {
    throw new Error("No customer key found to export");
  }
  const backup = {
    app: "ComputeCoin Proof-of-Privacy",
    version: "ccpp-v1",
    type: "customer-key-backup",
    publicKeyHex: key.publicKeyHex,
    privateKeyHex: key.privateKeyHex,
    createdAt: key.createdAt,
    notice:
      "CRITICAL: Keep this file private and safe. ComputeCoin cannot recover your private key. " +
      "Without it, your encrypted job outputs cannot be decrypted.",
  };
  return JSON.stringify(backup, null, 2);
}

export class UnbackedKeyError extends Error {
  constructor() {
    super(
      "This browser holds a different private key that has not been backed up. Download its backup " +
        "first: replacing it would make the results of jobs submitted with it unreadable."
    );
    this.name = "UnbackedKeyError";
  }
}

/**
 * Import a customer key from backup JSON or hex string.
 *
 * Refuses to replace a DIFFERENT key that was never backed up (that would silently destroy the
 * only copy), unless ``replaceUnbackedKey`` is set after the user explicitly confirmed it.
 */
export async function importCustomerKey(
  raw: string,
  { replaceUnbackedKey = false }: { replaceUnbackedKey?: boolean } = {}
): Promise<StoredCustomerKey> {
  let privateHex = "";
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed.privateKeyHex === "string") {
      privateHex = parsed.privateKeyHex.trim();
    }
  } catch {
    privateHex = raw.trim();
  }

  if (privateHex.startsWith("0x") || privateHex.startsWith("0X")) {
    privateHex = privateHex.slice(2);
  }

  const publicHex = x25519PublicFromPrivate(privateHex);
  const existing = await getCustomerKey();
  if (existing && existing.publicKeyHex !== publicHex && !existing.backedUp && !replaceUnbackedKey) {
    throw new UnbackedKeyError();
  }
  const record: StoredCustomerKey = {
    id: KEY_ID,
    privateKeyHex: privateHex,
    publicKeyHex: publicHex,
    createdAt: new Date().toISOString(),
    backedUp: true, // User already has the backup since they imported it
  };

  await saveCustomerKey(record);
  return record;
}

/**
 * Delete key from browser storage.
 */
export async function deleteCustomerKey(): Promise<void> {
  if (!hasIndexedDB()) {
    memoryStore = null;
    return;
  }
  const db = await openDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE_NAME, "readwrite");
    const store = tx.objectStore(STORE_NAME);
    const req = store.delete(KEY_ID);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });
}
