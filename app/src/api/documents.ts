import { apiGet, apiPatch, apiPost } from './client';
import type { Document, UpdateMetadataRequest } from './types';

export async function getDocument(vaultId: string, documentId: string): Promise<Document> {
  return apiGet<Document>(`/sage_vaults/${vaultId}/documents/${documentId}`);
}

export async function updateMetadata(
  vaultId: string,
  documentId: string,
  body: UpdateMetadataRequest,
): Promise<Document> {
  return apiPatch<Document>(`/sage_vaults/${vaultId}/documents/${documentId}/metadata`, body);
}

export async function openDocument(
  vaultId: string,
  documentId: string,
): Promise<{ opened: boolean; path: string }> {
  return apiPost(`/sage_vaults/${vaultId}/documents/${documentId}/open`, {});
}
