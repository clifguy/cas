import { apiGet, apiPost } from './client';
import type {
  PendingMetadataPage,
  StagingEdge,
  StagingEdgeConfirmResponse,
  StagingEdgeDismissResponse,
} from './types';

// The largest page the queue serves.
const PENDING_METADATA_PAGE_SIZE = 100;

// The whole queue as full rows: the review table needs each document's
// extracted-field annotations, which the light projection omits, and acts on
// every pending document, so a queue longer than one page is read page by
// page. An empty page ends the read even if the total says more remain, since
// confirmations elsewhere can shrink the queue between pages.
export async function listPendingMetadata(vaultId: string): Promise<PendingMetadataPage> {
  const items: PendingMetadataPage['items'] = [];
  let page: PendingMetadataPage;
  do {
    page = await apiGet<PendingMetadataPage>(
      `/sage_vaults/${vaultId}/pending-metadata?limit=${PENDING_METADATA_PAGE_SIZE}` +
        `&offset=${items.length}&response_mode=full`,
    );
    items.push(...page.items);
  } while (page.items.length > 0 && items.length < page.total_available);
  return { ...page, items, offset: 0, limit: items.length };
}

export async function listStagingEdges(vaultId: string): Promise<StagingEdge[]> {
  return apiGet<StagingEdge[]>(`/sage_vaults/${vaultId}/staging-edges`);
}

export async function confirmStagingEdge(
  vaultId: string,
  edgeId: string,
): Promise<StagingEdgeConfirmResponse> {
  return apiPost<StagingEdgeConfirmResponse>(`/sage_vaults/${vaultId}/staging-edges/${edgeId}/confirm`, {});
}

export async function dismissStagingEdge(
  vaultId: string,
  edgeId: string,
): Promise<StagingEdgeDismissResponse> {
  return apiPost<StagingEdgeDismissResponse>(`/sage_vaults/${vaultId}/staging-edges/${edgeId}/dismiss`, {});
}
