param(
    [Parameter(Mandatory=$true)][string]$SubscriptionId,
    [Parameter(Mandatory=$true)][string]$TrialGroup,
    [Parameter(Mandatory=$true)][string]$RunId,
    [Parameter(Mandatory=$true)][string]$ExpiresAt,
    [ValidateSet('Inspect','Expire')][string]$Action='Inspect'
)
$ErrorActionPreference='Stop'
if ($SubscriptionId -notmatch '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$' -or $RunId -notmatch '^[a-z][a-z0-9-]{2,19}$' -or $TrialGroup -cne "rg-ingest-perf-$RunId") { throw 'Invalid isolated cleanup identity' }
$expiry=[DateTimeOffset]::Parse($ExpiresAt)
if ($ExpiresAt -notmatch '(Z|[+-][0-9]{2}:[0-9]{2})$') { throw 'Timezone required' }
$tokenResponse=Invoke-RestMethod -Uri "$($env:IDENTITY_ENDPOINT)?resource=https%3A%2F%2Fmanagement.azure.com%2F&api-version=2019-08-01" -Headers @{ 'X-IDENTITY-HEADER'=$env:IDENTITY_HEADER; 'Metadata'='true' }
$headers=@{ Authorization="Bearer $($tokenResponse.access_token)" }
$groupPath="/subscriptions/$SubscriptionId/resourceGroups/$TrialGroup"
$root="https://management.azure.com$groupPath"
function Invoke-Arm([string]$Uri,[string]$Method='GET') {
    if (-not ($Uri.StartsWith("$root/",[StringComparison]::OrdinalIgnoreCase) -or $Uri.StartsWith("$root`?",[StringComparison]::OrdinalIgnoreCase))) { throw 'Cleanup request outside approved group' }
    Invoke-RestMethod -Method $Method -Uri $Uri -Headers $headers
}
try { $group=Invoke-Arm "$($root)?api-version=2024-03-01" }
catch {
    if ([int]$_.Exception.Response.StatusCode -eq 404) { Write-Output '{"kind":"expiry","outcome":"already_absent"}'; return }
    throw
}
if ($group.id -ine $groupPath -or $group.name -cne $TrialGroup -or $group.tags.benchmarkRun -cne $RunId -or $group.tags.expiresAt -cne $ExpiresAt) { throw 'Group ownership or expiry drift' }
$next="$root/resources?api-version=2021-04-01"
$count=0
while ($next) {
    $page=Invoke-Arm $next
    foreach ($item in $page.value) {
        if (-not $item.id.StartsWith("$groupPath/providers/",[StringComparison]::OrdinalIgnoreCase) -or $item.tags.benchmarkRun -cne $RunId -or $item.tags.expiresAt -cne $ExpiresAt) { throw 'Unowned resource prevents cleanup' }
        $count++
    }
    $next=$page.nextLink
}
if ($Action -eq 'Inspect') { Write-Output "{`"kind`":`"expiry_preflight`",`"outcome`":`"owned`",`"resources`":$count}"; return }
if ([DateTimeOffset]::UtcNow -lt $expiry) { throw 'Refusing deletion before approved expiry' }
# The identity is Contributor on this exact trial group only. No subscription,
# production, registry, site or evidence-controller deletion permission is held.
$null=Invoke-Arm "$($root)?api-version=2024-03-01" 'DELETE'
$deadline=[DateTimeOffset]::UtcNow.AddMinutes(45)
while ([DateTimeOffset]::UtcNow -lt $deadline) {
    try { $null=Invoke-Arm "$($root)?api-version=2024-03-01" }
    catch {
        if ([int]$_.Exception.Response.StatusCode -eq 404) { Write-Output '{"kind":"expiry","outcome":"deleted"}'; return }
        throw
    }
    Start-Sleep -Seconds 15
}
throw 'Trial deletion remains unconfirmed; controller is retained'
