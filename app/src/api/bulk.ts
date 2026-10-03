import { apiPost, pathSegment } from './client';
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
  return apiPost<BulkLifecycleResponse>(`/sage_vaults/${pathSegment(vaultId)}/lifecycles`, { items });
}

export async function bulkUpdateMetadata(
  vaultId: string,
  items: BulkMetadataItem[],
): Promise<BulkMetadataResponse> {
  return apiPost<BulkMetadataResponse>(`/sage_vaults/${pathSegment(vaultId)}/metadata`, { items });
}
