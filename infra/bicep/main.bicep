targetScope = 'resourceGroup'

param location string = resourceGroup().location
@minLength(2)
param baseName string = 'churn'
@minLength(2)
param env string = 'dev'
@description('Static Web Apps is not available in centralindia.')
param swaLocation string = 'eastasia'
@description('API image, pinned by digest, e.g. ghcr.io/user/churn-api@sha256:...')
param containerImage string
@description('Extra browser origins for CORS. The Static Web App origin is always added.')
param allowedOrigins string = ''
param jwtIssuer string = ''
param jwtAudience string = ''
param jwtJwksUrl string = ''
@description('Email for alerts and budget notices. Empty skips both.')
param alertEmail string = ''
@description('Monthly budget in USD.')
param budgetAmount int = 10
param budgetStart string = utcNow('yyyy-MM-01')
@description('Azure ML workspace and CPU cluster for retraining. Cluster scales to zero.')
param deployMl bool = true
@description('Blob event subscription. Needs the function code published first, so it is off on the first pass.')
param deployEventSubscription bool = false
param retrainRowThreshold int = 1000

var prefix = '${baseName}-${env}'
var unique = uniqueString(resourceGroup().id)
var storageName = take(toLower('${baseName}${env}${unique}'), 24)
var mlStorageName = take(toLower('${baseName}${env}ml${unique}'), 24)
var keyVaultName = take(toLower('${baseName}${env}kv${unique}'), 24)
var functionAppName = take(toLower('${prefix}-fn-${unique}'), 60)
var hostStorageName = take(toLower('${baseName}${env}fn${unique}'), 24)
var cosmosName = take(toLower('${prefix}-cosmos-${unique}'), 44)
var swaOrigin = 'https://${swa.properties.defaultHostname}'
var corsOrigins = empty(allowedOrigins) ? swaOrigin : '${swaOrigin},${allowedOrigins}'

var roles = {
  blobReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
  blobContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  blobOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  queueContributor: '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
  tableContributor: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
  amlDataScientist: 'f6c7c914-8db3-469d-8ca1-694a8f32e121'
}

func roleId(id string) string => subscriptionResourceId('Microsoft.Authorization/roleDefinitions', id)

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    isHnsEnabled: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource rawContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'raw'
  properties: { publicAccess: 'None' }
}

resource modelsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'models'
  properties: { publicAccess: 'None' }
}

resource hostStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: hostStorageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
  }
}

resource hostBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: hostStorage
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: hostBlobService
  name: 'app-package'
  properties: { publicAccess: 'None' }
}

resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'prune-retrain-decisions'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: { blobTypes: ['blockBlob'], prefixMatch: ['models/retrain_decision-'] }
            actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 90 } } }
          }
        }
      ]
    }
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${prefix}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: json('0.15') }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${prefix}-ai'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource cosmos 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: cosmosName
  location: location
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    enableFreeTier: true
    disableLocalAuth: true
    consistencyPolicy: { defaultConsistencyLevel: 'Session' }
    locations: [{ locationName: location, failoverPriority: 0, isZoneRedundant: false }]
  }
}

resource cosmosDb 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmos
  name: 'churn'
  properties: { resource: { id: 'churn' } }
}

resource predictions 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: cosmosDb
  name: 'predictions'
  properties: {
    resource: {
      id: 'predictions'
      partitionKey: { paths: ['/day'], kind: 'Hash' }
      defaultTtl: 2592000
    }
    options: { throughput: 400 }
  }
}

resource cae 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${prefix}-cae'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: '${prefix}-api'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: cae.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8000, transport: 'http' }
    }
    template: {
      containers: [
        {
          name: 'api'
          image: containerImage
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'ALLOWED_ORIGINS', value: corsOrigins }
            { name: 'JWT_ISSUER', value: jwtIssuer }
            { name: 'JWT_AUDIENCE', value: jwtAudience }
            { name: 'JWT_JWKS_URL', value: jwtJwksUrl }
            { name: 'STORAGE_ACCOUNT_NAME', value: storage.name }
            { name: 'COSMOS_ENDPOINT', value: cosmos.properties.documentEndpoint }
          ]
          probes: [
            { type: 'Startup', httpGet: { path: '/ready', port: 8000 }, periodSeconds: 3, failureThreshold: 20 }
            { type: 'Readiness', httpGet: { path: '/ready', port: 8000 }, periodSeconds: 10 }
            { type: 'Liveness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 30 }
          ]
        }
      ]
      scale: {
        minReplicas: 0
        maxReplicas: 1
        rules: [{ name: 'http', http: { metadata: { concurrentRequests: '20' } } }]
      }
    }
  }
}

resource apiBlobReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(modelsContainer.id, containerApp.id, roles.blobReader)
  scope: modelsContainer
  properties: {
    roleDefinitionId: roleId(roles.blobReader)
    principalId: containerApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apiCosmosRole 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmos
  name: guid(cosmos.id, containerApp.id, 'data-contributor')
  properties: {
    roleDefinitionId: '${cosmos.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002'
    principalId: containerApp.identity.principalId
    scope: cosmos.id
  }
}

resource functionPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${prefix}-flexplan'
  location: location
  kind: 'functionapp'
  sku: { name: 'FC1', tier: 'FlexConsumption' }
  properties: { reserved: true }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: { type: 'SystemAssigned' }
  properties: {
    serverFarmId: functionPlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${hostStorage.properties.primaryEndpoints.blob}${deploymentContainer.name}'
          authentication: { type: 'SystemAssignedIdentity' }
        }
      }
      scaleAndConcurrency: { maximumInstanceCount: 40, instanceMemoryMB: 2048 }
      runtime: { name: 'python', version: '3.11' }
    }
    siteConfig: {
      appSettings: [
        { name: 'AzureWebJobsStorage__accountName', value: hostStorage.name }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
        { name: 'STORAGE_ACCOUNT_NAME', value: storage.name }
        { name: 'RETRAIN_ROW_THRESHOLD', value: string(retrainRowThreshold) }
        { name: 'AML_TRIGGER_ENABLED', value: string(deployMl) }
        { name: 'AML_SUBSCRIPTION_ID', value: subscription().subscriptionId }
        { name: 'AML_RESOURCE_GROUP', value: resourceGroup().name }
        { name: 'AML_WORKSPACE', value: deployMl ? mlWorkspace.name : '' }
        { name: 'AML_COMPUTE', value: 'cpu-cluster' }
      ]
    }
  }
}

resource funcHostRoles 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for role in ['blobOwner', 'queueContributor', 'tableContributor']: {
  name: guid(hostStorage.id, functionApp.id, roles[role])
  scope: hostStorage
  properties: {
    roleDefinitionId: roleId(roles[role])
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}]

resource funcRawReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rawContainer.id, functionApp.id, roles.blobReader)
  scope: rawContainer
  properties: {
    roleDefinitionId: roleId(roles.blobReader)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcModelsContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(modelsContainer.id, functionApp.id, roles.blobContributor)
  scope: modelsContainer
  properties: {
    roleDefinitionId: roleId(roles.blobContributor)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource swa 'Microsoft.Web/staticSites@2022-09-01' = {
  name: '${prefix}-web'
  location: swaLocation
  sku: { name: 'Free', tier: 'Free' }
  properties: {}
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (deployMl) {
  name: keyVaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    accessPolicies: []
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
  }
}

resource mlStorage 'Microsoft.Storage/storageAccounts@2023-01-01' = if (deployMl) {
  name: mlStorageName
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
  }
}

resource mlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' = if (deployMl) {
  name: '${prefix}-ml'
  location: location
  identity: { type: 'SystemAssigned' }
  sku: { name: 'Basic', tier: 'Basic' }
  properties: {
    friendlyName: 'Churn retraining'
    storageAccount: mlStorage.id
    keyVault: keyVault.id
    applicationInsights: appInsights.id
  }
}

resource cluster 'Microsoft.MachineLearningServices/workspaces/computes@2024-04-01' = if (deployMl) {
  parent: mlWorkspace
  name: 'cpu-cluster'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: 'STANDARD_DS2_V2'
      vmPriority: 'Dedicated'
      scaleSettings: { minNodeCount: 0, maxNodeCount: 1, nodeIdleTimeBeforeScaleDown: 'PT2M' }
      remoteLoginPortPublicAccess: 'Disabled'
    }
  }
}

resource rawStore 'Microsoft.MachineLearningServices/workspaces/datastores@2024-04-01' = if (deployMl) {
  parent: mlWorkspace
  name: 'raw'
  properties: {
    datastoreType: 'AzureDataLakeGen2'
    accountName: storage.name
    filesystem: 'raw'
    credentials: { credentialsType: 'None' }
    serviceDataAccessAuthIdentity: 'WorkspaceSystemAssignedIdentity'
  }
}

resource modelsStore 'Microsoft.MachineLearningServices/workspaces/datastores@2024-04-01' = if (deployMl) {
  parent: mlWorkspace
  name: 'models'
  properties: {
    datastoreType: 'AzureDataLakeGen2'
    accountName: storage.name
    filesystem: 'models'
    credentials: { credentialsType: 'None' }
    serviceDataAccessAuthIdentity: 'WorkspaceSystemAssignedIdentity'
  }
}

resource mlWorkspaceBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployMl) {
  name: guid(storage.id, mlWorkspace.id, roles.blobContributor)
  scope: storage
  properties: {
    roleDefinitionId: roleId(roles.blobContributor)
    principalId: mlWorkspace!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource mlClusterBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployMl) {
  name: guid(storage.id, cluster.id, roles.blobContributor)
  scope: storage
  properties: {
    roleDefinitionId: roleId(roles.blobContributor)
    principalId: cluster!.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource funcMlRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployMl) {
  name: guid(mlWorkspace.id, functionApp.id, roles.amlDataScientist)
  scope: mlWorkspace
  properties: {
    roleDefinitionId: roleId(roles.amlDataScientist)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource blobTopic 'Microsoft.EventGrid/systemTopics@2022-06-15' = if (deployEventSubscription) {
  name: '${prefix}-blob-events'
  location: location
  properties: {
    source: storage.id
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource rawUploadSub 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2022-06-15' = if (deployEventSubscription) {
  parent: blobTopic
  name: 'raw-csv-uploaded'
  properties: {
    destination: {
      endpointType: 'AzureFunction'
      properties: { resourceId: '${functionApp.id}/functions/on_raw_upload' }
    }
    filter: {
      includedEventTypes: ['Microsoft.Storage.BlobCreated']
      subjectBeginsWith: '/blobServices/default/containers/raw/'
      subjectEndsWith: '.csv'
    }
  }
}

resource alertGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (!empty(alertEmail)) {
  name: '${prefix}-alerts'
  location: 'global'
  properties: {
    groupShortName: 'churn'
    enabled: true
    emailReceivers: [{ name: 'owner', emailAddress: alertEmail, useCommonAlertSchema: true }]
  }
}

resource api5xx 'Microsoft.Insights/metricAlerts@2018-03-01' = if (!empty(alertEmail)) {
  name: '${prefix}-api-5xx'
  location: 'global'
  properties: {
    severity: 2
    enabled: true
    scopes: [containerApp.id]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'server-errors'
          metricName: 'Requests'
          dimensions: [{ name: 'statusCodeCategory', operator: 'Include', values: ['5xx'] }]
          operator: 'GreaterThan'
          threshold: 5
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    actions: [{ actionGroupId: alertGroup.id }]
  }
}

resource retrainFailed 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (!empty(alertEmail)) {
  name: '${prefix}-retrain-failed'
  location: location
  kind: 'LogAlert'
  properties: {
    severity: 2
    enabled: true
    scopes: [appInsights.id]
    evaluationFrequency: 'PT15M'
    windowSize: 'PT15M'
    criteria: {
      allOf: [
        {
          query: 'traces | where message has "retrain job failed"'
          timeAggregation: 'Count'
          operator: 'GreaterThan'
          threshold: 0
          failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
        }
      ]
    }
    actions: { actionGroups: [alertGroup.id] }
  }
}

resource budget 'Microsoft.Consumption/budgets@2023-05-01' = if (!empty(alertEmail)) {
  name: '${prefix}-monthly'
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: { startDate: budgetStart }
    notifications: {
      half: { enabled: true, operator: 'GreaterThan', threshold: 50, contactEmails: [alertEmail] }
      nearly: { enabled: true, operator: 'GreaterThan', threshold: 90, contactEmails: [alertEmail] }
    }
  }
}

output apiUrl string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output webUrl string = swaOrigin
output functionAppName string = functionApp.name
output storageAccount string = storage.name
output swaName string = swa.name
