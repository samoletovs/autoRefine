// autoRefine — scheduled evaluation job.
//
// Moved off GitHub Actions: the daily evaluate run is a sequential loop of Foundry calls
// across every project, so it billed ~43 minutes of Actions time a day (1,744 min/month,
// 58% of the account's included minutes) while mostly sitting blocked on API responses.
//
// A Container Apps Job is the right shape for it — cron-triggered, scales to zero, no
// function timeout ceiling, and the agent shells out to `git clone` per project, which the
// Python Functions runtime image cannot do. Reuses the existing `cae-agents` environment
// rather than standing up another one.
//
// Cost: a full pass over all 20 projects measured 116 min. At 0.5 vCPU / 1Gi that is
// ~106k of the 180k free vCPU-seconds and ~212k of the 360k free GiB-seconds per month.
// Doubling either dimension would overrun the grant for no gain — the loop spends almost
// all of that time blocked on Foundry responses, not computing.

targetScope = 'resourceGroup'

@description('Existing Container Apps environment to run in.')
param environmentName string = 'cae-agents'

@description('Key Vault holding github-pat.')
param githubVaultName string = 'kv-agents-s6vbks3oteo4y'

@description('Key Vault holding nauro-bot-token (lives in another resource group).')
param notifyVaultName string = 'kv-mindme-ymcpt'

@description('Foundry *project* endpoint the agent calls.')
// Must be the project endpoint (.services.ai.azure.com/api/projects/<project>), not the
// account endpoint (.cognitiveservices.azure.com) — the Agents API returns a bare 404 on
// the latter, which reads like a missing model rather than a wrong URL.
param foundryEndpoint string = 'https://foundrylab-aiservices.services.ai.azure.com/api/projects/foundrylab'

@description('Telegram chat id for notifications. Not a secret, but environment-specific.')
param nauroChatId string

@description('UTC cron for the evaluation run. Daily 06:00, matching the retired workflow.')
param cronExpression string = '0 6 * * *'

@description('Set false to pause the schedule without deleting the job.')
param scheduleEnabled bool = true

var jobName = 'job-autorefine'
var githubVaultUri = 'https://${githubVaultName}${environment().suffixes.keyvaultDns}/'
var notifyVaultUri = 'https://${notifyVaultName}${environment().suffixes.keyvaultDns}/'

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource job 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: resourceGroup().location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    environmentId: cae.id
    configuration: {
      triggerType: 'Schedule'
      // The run is one long sequential pass; a second concurrent copy would double-file the
      // same idea cards, so retries are serial and overlap is not allowed.
      // A full pass measured 116 min and the length moves with how many projects are due
      // for a new idea card, so this is 3h rather than a snug fit — a timeout throws away
      // the entire run, and an unused ceiling costs nothing on a job that scales to zero.
      replicaTimeout: 10800
      replicaRetryLimit: 1
      scheduleTriggerConfig: {
        cronExpression: scheduleEnabled ? cronExpression : '0 0 31 2 *' // 31 Feb = never
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: [
        {
          name: 'github-pat'
          keyVaultUrl: '${githubVaultUri}secrets/github-pat'
          identity: 'system'
        }
        {
          name: 'nauro-bot-token'
          keyVaultUrl: '${notifyVaultUri}secrets/nauro-bot-token'
          identity: 'system'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'autorefine'
          // Public image, and the full (non-slim) tag because the agent shells out to git.
          image: 'python:3.12'
          resources: {
            // The run is I/O-bound — it waits on Foundry far more than it computes — so the
            // smallest size is enough and keeps the run inside the free grant. Raise this
            // only against a measured memory or CPU limit, not a hunch: 1.0/2Gi was tried
            // once on an OOM theory that turned out to be a missing npm.
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'GH_TOKEN', secretRef: 'github-pat' }
            { name: 'GITHUB_TOKEN', secretRef: 'github-pat' }
            { name: 'NAURO_BOT_TOKEN', secretRef: 'nauro-bot-token' }
            { name: 'NAURO_CHAT_ID', value: nauroChatId }
            { name: 'FOUNDRY_PROJECT_ENDPOINT', value: foundryEndpoint }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscription().subscriptionId }
            // Observe-first, mirroring the workflow it replaces: propose, never file.
            { name: 'AUTOREFINE_FUNCTIONAL_MODE', value: 'cards' }
            { name: 'AUTOREFINE_TIER', value: 'high' }
          ]
          command: ['/bin/sh', '-c']
          args: [
            loadTextContent('run-autorefine.sh')
          ]
        }
      ]
    }
  }
}

resource githubVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: githubVaultName
}

// Key Vault Secrets User — the job reads github-pat at start-up.
resource githubVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: githubVault
  name: guid(githubVault.id, job.id, 'kv-secrets-user')
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '4633458b-17de-408a-b874-0445c86b69e6'
    )
    principalId: job.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

output jobName string = job.name
output principalId string = job.identity.principalId
