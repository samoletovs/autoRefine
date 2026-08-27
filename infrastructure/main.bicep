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
// Cost, measured 2026-08-20..27 over eight runs: 7.4 / 14.0 / 24.4 / 28.1 / 85.5 / 103.0 /
// 108.3 / 167.8 minutes — a 23x spread, median ~57 min. At 0.5 vCPU / 1Gi and one run a
// day that is roughly 61k of the 180k free vCPU-seconds and 121k of the 360k free
// GiB-seconds per month, about a third of each grant. The figure this replaced was a
// single 116-minute sample quoted as though it were a property; it read as 59% of both.
// Doubling either dimension would still buy nothing — the loop spends almost all of that
// time blocked on Foundry responses, not computing. Re-measure rather than quote: the
// window spans the activity gate, so even this spread is two regimes, not one.

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
    // Declared, not left to inference. `cae-agents` is a workload-profiles environment
    // offering exactly one profile — Consumption — and all seven workloads in it run on
    // that profile, this job included. Leaving the property out kept the deployed value
    // out of the template, so `what-if` reported
    //   properties.workloadProfileName: 'Consumption' -> null
    // on every run: a permanent red herring sitting next to the one delta a deploy is
    // actually for. Whether the RP would honour that null or re-default it is not worth
    // resolving, because naming the profile the job already runs on is correct either
    // way — and it is what makes a redeploy a true no-op apart from the script below.
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Schedule'
      // The run is one long sequential pass; a second concurrent copy would double-file the
      // same idea cards, so retries are serial and overlap is not allowed.
      //
      // 6h, raised from 3h on 2026-08-27. The old value was sized on a single 116-minute
      // sample and its comment called 3h "rather than a snug fit". Eight days of real
      // execution history say otherwise: 7.4, 14.0, 24.4, 28.1, 85.5, 103.0, 108.3 and
      // 167.8 minutes. The longest run reached 93.2% of the 180-minute ceiling, which is a
      // snug fit by any reading, and the comment's own two reasons — a timeout throws away
      // the entire run, and an unused ceiling costs nothing on a job that scales to zero —
      // both argue for a larger number, not that one.
      //
      // This is a backstop, not a prediction, so it is not sized off the distribution. It
      // should sit far enough above any plausible run to catch only pathology, and the two
      // errors are not symmetric: too generous costs a few percent of a free grant on a day
      // that never comes, while too tight costs the whole sweep *and* leaks a Foundry agent,
      // because a hard kill never reaches main.py's `finally` (see AGENTS.md, "Foundry agent
      // lifecycle").
      //
      // Priced, so the next person can re-derive rather than trust: at 0.5 vCPU / 1Gi a run
      // that burned the full ceiling costs 21,600 x 0.5 = 10,800 vCPU-seconds and 21,600
      // GiB-seconds, which is 6.0% of the 180k free vCPU-seconds and 6.0% of the 360k free
      // GiB-seconds for the month. A ceiling that is never reached bills nothing at all.
      //
      // It also bounds the whole execution rather than each attempt: "The replicaTimeout
      // setting takes precedence if it expires before all retries occur"
      // (learn.microsoft.com/azure/container-apps/jobs, "Advanced job configuration"). So
      // the worst case is 6h total, not 6h per try — and the deliberate single retry below
      // only has room to accomplish anything if the ceiling is well clear of a normal run.
      // Under the old 3h a run failing at 167.8 min left the retry 12 minutes, which would
      // have timed out having done nothing.
      //
      // Provisional. Eight samples spanning the activity-gate change is not a stable
      // distribution — the two shortest are post-gate — and the cost rows now landing in
      // reports/cost carry per-run mode and rounds, which will say whether a long run is
      // many projects or a few slow ones. That decides whether this ceiling or the gate is
      // the thing to move. Re-measure before quoting any number in this comment.
      replicaTimeout: 21600
      // Not raised with the ceiling, deliberately: a retry re-runs the entire sweep.
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
          // No node here, and none needed: the dependency check reads Dependabot alerts
          // over the API rather than shelling out to `npm audit`. Adding node to "fix"
          // that check would be re-solving a problem that no longer exists — see
          // AGENTS.md, "The dependency check".
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
            // `cards`, NOT `propose`: this files each idea as a `needs-approval` memo and
            // sends a Telegram approval card. It does file issues — nothing builds until a
            // human taps 👍, which is where the observe-first guarantee actually lives.
            // `propose` is the mode that files nothing; do not read the two as synonyms.
            { name: 'AUTOREFINE_FUNCTIONAL_MODE', value: 'cards' }
            { name: 'AUTOREFINE_TIER', value: 'high' }
          ]
          command: ['/bin/sh', '-c']
          args: [
            // BAKED AT DEPLOY TIME — the one thing here that is not read from master.
            // loadTextContent inlines the file into the ARM template, so the job runs
            // whatever copy the last deployment captured. Editing run-autorefine.sh and
            // merging it changes nothing in production until this template is redeployed,
            // and nothing reports that: no error, no failed run, no missing output that
            // anyone is watching for.
            //
            // It has already happened once. The cost-telemetry block added in #12 sat
            // merged and inert while the 06:00 job kept executing the pre-#12 script, and
            // the only symptom was a file that never appeared in reports/cost.
            //
            // The Python is the opposite and that asymmetry is the trap: the script
            // git-clones autoRefine at start-up, so agent/ really is whatever is on
            // master. Only this one file is frozen.
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
