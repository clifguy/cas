import { apiGet, apiPost } from './client';
import type {
  BulkMetadataItemResult,
  BulkMetadataResponse,
  Document,
  UpdateMetadataRequest,
} from './types';

export async function getDocument(vaultId: string, documentId: string): Promise<Document> {
  return apiGet<Document>(`/sage_vaults/${vaultId}/documents/${documentId}`);
}

export async function updateMetadata(
  vaultId: string,
  documentId: string,
  body: UpdateMetadataRequest,
): Promise<Document> {
  // CAS-ADR-029 v4 plural-noun: single endpoint POST /sage_vaults/{id}/metadata
  // takes an items array; the singleton-shaped caller signature is preserved
  // by wrapping the body as a length-1 items collection and unwrapping the
  // per-item result envelope.
  const response = await apiPost<BulkMetadataResponse>(
    `/sage_vaults/${vaultId}/metadata`,
    { items: [{ document_id: documentId, ...body }] },
  );
  const item: BulkMetadataItemResult | undefined = response.results[0];
  if (!item || item.status !== 'success' || !item.document) {
    const err = item?.error;
    throw new Error(err ? `${err.error}: ${err.message}` : 'update_metadata failed');
  }
  return item.document;
}

export async function openDocument(
  vaultId: string,
  documentId: string,
): Promise<{ opened: boolean; path: string }> {
  return apiPost(`/sage_vaults/${vaultId}/documents/${documentId}/open`, {});
}
