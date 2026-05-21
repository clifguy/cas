import { apiPost } from './client';
import type {
  BulkLifecycleItem,
  BulkLifecycleResponse,
  BulkMetadataItem,
  BulkMetadataResponse,
} from './types';

export async function bulkSetLifecycle(
  vaultId: string,
  items: BulkLifecycleItem[],
): Promise<BulkLifecycleResponse> {
  return apiPost<BulkLifecycleResponse>(`/sage_vaults/${vaultId}/lifecycle/bulk`, { items });
}

export async function bulkUpdateMetadata(
  vaultId: string,
  items: BulkMetadataItem[],
): Promise<BulkMetadataResponse> {
  return apiPost<BulkMetadataResponse>(`/sage_vaults/${vaultId}/metadata/bulk`, { items });
}
