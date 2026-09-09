#!/usr/bin/env bash
# A durable resource-group tag holds the fence across failed workflow runs.
# All mutating workflows share a tenant concurrency group as well.
set -euo pipefail
[ "$#" -ge 2 ] || { echo 'usage: postgres-migration-guard.sh <deploy|maintenance|migration> <resource-group> [generation]' >&2; exit 2; }
purpose="$1"
group="$2"
generation="${3:-}"
exists="$(az group exists --name "$group" -o tsv)"
case "$exists" in
  false) [ "$purpose" = deploy ] && [ -z "$generation" ] && exit 0; exit 1 ;;
  true) ;;
  *) echo 'could not establish resource group existence' >&2; exit 1 ;;
esac
# GitHub cancellation releases its concurrency slot, not the Azure execution.
# Check the actual job before any workflow can reconfigure it or its consumers.
migration_jobs="$(az containerapp job list --resource-group "$group" --query "[?starts_with(name, 'job-pg-migration-')].name" -o tsv)"
for migration_job in $migration_jobs; do
  statuses="$(az containerapp job execution list --resource-group "$group" --name "$migration_job" --query '[].properties.status' -o tsv)"
  for status in $statuses; do
    case "$status" in
      Succeeded|Failed|Stopped) ;;
      *) echo 'database job remains active; wait for or stop its Azure execution' >&2; exit 1 ;;
    esac
  done
done
state="$(az group show --name "$group" --query 'tags.casPostgresMigration' -o tsv)"
case "$purpose" in
  maintenance)
    [[ -z "$state" || "$state" == serving:* ]] || { echo 'database migration fence is held' >&2; exit 1; } ;;
  deploy)
    if [ -n "$state" ]; then
      [ -n "$generation" ] && { [ "$state" = "verified:$generation" ] || [ "$state" = "serving:$generation" ] || [ "$state" = "cutover:$generation" ]; } || { echo 'migration is not verified for the selected generation' >&2; exit 1; }
    elif [ -n "$generation" ]; then
      selected="$(az deployment sub show --name "${ENVIRONMENT_NAME:?}" --query properties.outputs.postgresServerName.value -o tsv)"
      [[ "$selected" == psql-*-"$generation" ]] || { echo 'replacement has not been verified' >&2; exit 1; }
    fi ;;
  migration)
    [[ "$generation" =~ ^[a-z][a-z0-9-]{0,11}$ ]] || { echo 'invalid generation' >&2; exit 2; }
    [ -z "$state" ] || [ "$state" = "copying:$generation" ] || [ "$state" = "verified:$generation" ] || { echo 'another migration owns the fence' >&2; exit 1; } ;;
  *) echo 'unknown migration guard purpose' >&2; exit 2 ;;
esac
